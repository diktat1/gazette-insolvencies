"""
Core orchestrator: fetches notices, enriches each one, scores them,
and returns fully-analysed results ready for the email report.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from src import config
from src.gazette_feed import GazetteEntry, fetch_latest_notices
from src.notice_parser import parse_notice
from src.companies_house import lookup_by_number, search_by_name, get_officers
from src.website_finder import find_website, build_google_search_url
from src.opportunity_scorer import score_opportunity
from src.email_report import AnalysedNotice
from src.db import is_notice_processed, mark_notice_processed
from src.sector_utils import get_sector_from_sic, estimate_key_assets

logger = logging.getLogger(__name__)


def analyse_notices(lookback_days: Optional[int] = None) -> list[AnalysedNotice]:
    """
    Full pipeline:
    1. Fetch new insolvency notices from the Gazette
    2. Skip already-processed notices
    3. Parse each notice to extract structured data
    4. Look up the company on Companies House
    5. Try to find the company's website (web search + cross-check)
    6. Score the opportunity
    7. Return fully-enriched notices sorted by score
    """
    entries = fetch_latest_notices(lookback_days)
    logger.info("Fetched %d raw notices from the Gazette", len(entries))

    # Skip already-processed notices up front (serial; SQLite reads).
    fresh = [e for e in entries if not is_notice_processed(e.notice_id)]
    logger.info("Analysing %d new notices (%d already processed)",
                len(fresh), len(entries) - len(fresh))

    # Enrich notices concurrently - the per-notice work is I/O-bound (Companies
    # House + website lookups). CH calls are globally rate-limited inside the
    # CH client, so this stays within API limits. DB writes (mark_processed)
    # happen back on the main thread to keep SQLite single-writer.
    results: list[AnalysedNotice] = []
    workers = int(os.getenv("ANALYSE_CONCURRENCY", "8"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {ex.submit(_analyse_single, e): e for e in fresh}
        for fut in as_completed(future_map):
            entry = future_map[fut]
            try:
                results.append(fut.result())
            except Exception:
                logger.exception("Error processing notice %s", entry.notice_id)
            mark_notice_processed(entry.notice_id, entry.title, entry.published)

    # Filter by minimum score
    if config.MIN_OPPORTUNITY_SCORE > 0:
        results = [r for r in results if r.opportunity_score >= config.MIN_OPPORTUNITY_SCORE]

    # Sort by heuristic score descending (baseline). LLM triage runs in main.py
    # once all country feeds have been appended, so a single triage pass covers
    # UK/RO/US/TR/IN/MY together and the subject line can surface L1/L2 counts.
    results.sort(key=lambda r: r.opportunity_score, reverse=True)

    logger.info("Analysis complete: %d notices ready for report", len(results))
    return results


def _analyse_single(entry: GazetteEntry) -> AnalysedNotice:
    """Analyse a single Gazette entry end-to-end."""
    notice = AnalysedNotice()

    # -----------------------------------------------------------------------
    # Step 1: Parse the notice HTML
    # -----------------------------------------------------------------------
    parsed = parse_notice(entry.title, entry.content_html, entry.notice_type)

    notice.notice_id = entry.notice_id
    # Clean up notice URL - remove data.ttl suffix if present
    notice_url = entry.notice_url
    if notice_url and "/data.ttl" in notice_url:
        notice_url = notice_url.replace("/data.ttl", "")
    notice.notice_url = notice_url
    notice.notice_type = entry.notice_type or parsed.notice_type_label
    notice.published_date = entry.published
    notice.company_name = parsed.company_name
    notice.company_number = parsed.company_number
    notice.trading_name = parsed.trading_name
    notice.registered_address = parsed.registered_address
    notice.court_name = parsed.court_name
    notice.court_case_number = parsed.court_case_number
    notice.practitioners = parsed.practitioners

    # -----------------------------------------------------------------------
    # Step 2: Companies House lookup
    # -----------------------------------------------------------------------
    profile = None
    if parsed.company_number:
        profile = lookup_by_number(parsed.company_number)

    # Fall back to name search if no number found or lookup failed
    if not profile and parsed.company_name:
        profile = search_by_name(parsed.company_name)

    if profile:
        notice.company_number = notice.company_number or profile.company_number
        notice.company_name = notice.company_name or profile.company_name
        notice.ch_status = profile.company_status
        notice.ch_type = profile.company_type
        notice.ch_sic_codes = profile.sic_codes
        notice.ch_url = profile.companies_house_url
        notice.ch_has_charges = profile.has_charges
        notice.ch_accounts_type = profile.last_accounts_type
        notice.ch_created = profile.date_of_creation

        # New: filing history and insolvency data
        notice.ch_filing_history_url = profile.filing_history_url
        notice.ch_total_filings = profile.total_filings
        notice.ch_recent_filings = profile.recent_filings
        notice.ch_insolvency_cases = profile.insolvency_cases
        notice.ch_total_charges = profile.total_charges
        notice.ch_outstanding_charges = profile.outstanding_charges
        notice.ch_is_phantom = profile.is_likely_phantom
        notice.ch_phantom_reasons = profile.phantom_reasons

        # Prefer Companies House address if we didn't get one from the notice
        if not notice.registered_address:
            notice.registered_address = profile.registered_address

    # -----------------------------------------------------------------------
    # Step 3: Website lookup (web search + cross-check)
    # -----------------------------------------------------------------------
    # Skip website check if SKIP_WEBSITE_CHECK is set (speeds up test mode)
    import os
    if os.getenv('SKIP_WEBSITE_CHECK', '').lower() in ('true', '1', 'yes'):
        website = None
        logger.debug("Skipping website check (SKIP_WEBSITE_CHECK=true)")
    else:
        website = find_website(
            notice.company_name,
            registered_address=notice.registered_address,
            company_number=notice.company_number,
        )
    notice.website_url = website
    notice.google_search_url = build_google_search_url(notice.company_name)

    # -----------------------------------------------------------------------
    # Step 4: Sector identification and asset estimation
    # -----------------------------------------------------------------------
    if notice.ch_sic_codes:
        sector_name, sector_code = get_sector_from_sic(notice.ch_sic_codes)
        notice.sector = sector_name
        notice.sector_code = sector_code

        notice.estimated_assets = estimate_key_assets(
            notice.ch_sic_codes,
            has_charges=notice.ch_has_charges,
            accounts_type=notice.ch_accounts_type,
        )

    # -----------------------------------------------------------------------
    # Step 5: Extract IP email and generate draft email
    # -----------------------------------------------------------------------
    if notice.practitioners:
        for p in notice.practitioners:
            if hasattr(p, 'email') and p.email:
                notice.ip_email = p.email
                break

        # Generate draft email for the IP
        ip_name = notice.practitioners[0].name if notice.practitioners else "Sir/Madam"
        notice.draft_email_subject = f"Expression of Interest - {notice.company_name}"
        notice.draft_email_body = _generate_draft_email(notice, ip_name)

    # -----------------------------------------------------------------------
    # Step 6: Score the opportunity
    # -----------------------------------------------------------------------
    assessment = score_opportunity(
        parsed,
        profile,
        has_website=website is not None,
    )
    notice.opportunity_score = assessment.score
    notice.opportunity_category = assessment.category
    notice.opportunity_signals = assessment.signals

    return notice


def _generate_draft_email(notice, ip_name: str) -> str:
    """Generate a draft email expressing interest to the IP."""
    assets_str = ", ".join(notice.estimated_assets[:3]) if notice.estimated_assets else "the business and its assets"

    return f"""Dear {ip_name},

I am writing to express my interest in acquiring assets or the business of {notice.company_name} (Company No: {notice.company_number or 'N/A'}).

I understand the company is currently in {notice.notice_type or 'insolvency proceedings'} and I would be keen to discuss:

- The availability of {assets_str}
- Any ongoing business operations that may be available for sale
- Timeline and process for submitting expressions of interest

I am a serious buyer with funds available to move quickly on suitable opportunities.

Please could you provide further details on:
1. What assets/business operations are available for sale
2. The process and timeline for sale
3. Any information memorandum or asset list available

I would be happy to provide proof of funds and sign any required NDAs.

I look forward to hearing from you.

Kind regards,
[Your Name]
[Your Contact Details]"""
