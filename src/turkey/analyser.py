"""Orchestrate the Turkish pipeline into AnalysedNotice objects.

fetch (ilan.gov.tr AdsByFilter) -> enrich top candidates (detail call) -> score
-> emit AnalysedNotice, so TR opportunities drop straight into the daily report.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from src import config
from src.email_report import AnalysedNotice
from src.db import is_notice_processed, mark_notice_processed
from src.turkey.feed import fetch_tr_entries, enrich_tr_entry
from src.turkey.scorer import score_tr

logger = logging.getLogger(__name__)


@dataclass
class TrPractitioner:
    name: str = ""
    role: str = ""
    firm: str = ""
    email: str = ""
    phone: str = ""


def analyse_turkey_notices(lookback_days: Optional[int] = None) -> list[AnalysedNotice]:
    lookback = lookback_days if lookback_days is not None else config.TURKEY_LOOKBACK_DAYS
    entries = fetch_tr_entries(lookback_days=lookback, max_companies=config.TURKEY_MAX_COMPANIES)
    logger.info("Turkey: fetched %d bankruptcy notices (lookback=%d days)", len(entries), lookback)

    fresh = [e for e in entries if not is_notice_processed(e.notice_id)]
    logger.info("Turkey: %d new after dedup", len(fresh))
    if not fresh:
        return []

    results: list[AnalysedNotice] = []
    for e in fresh:
        try:
            # Detail call adds the body text + asset/auction price the scorer wants.
            enrich_tr_entry(e)
            assessment = score_tr(e)

            n = AnalysedNotice()
            n.country = "TR"
            n.notice_id = e.notice_id
            n.notice_url = e.url
            n.notice_type = e.title or "Bankruptcy-law notice (Turkey)"
            n.published_date = e.published
            # No central company registry lookup here, so the court/office is the
            # named party; the debtor company sits in the notice body.
            n.company_name = "🇹🇷 " + (e.debtor or e.title or e.advertiser)
            n.company_number = e.ad_no
            n.registered_address = ", ".join(x for x in (e.county, e.city) if x)
            n.court_name = e.advertiser
            n.court_case_number = e.dosya
            n.ch_url = e.url
            n.ch_status = "Bankruptcy proceedings"
            if e.estimated_price:
                n.estimated_assets = [f"Estimated value {e.estimated_price}"]

            n.opportunity_score = assessment["score"]
            n.opportunity_category = assessment["category"]
            n.opportunity_signals = assessment["signals"]

            # The advertiser is the bankruptcy office/court handling the case -
            # surface it as the contact route, with a search link.
            if e.advertiser and assessment["category"] in ("HIGH", "MEDIUM"):
                n.practitioners = [TrPractitioner(
                    name=e.advertiser, role="Bankruptcy office / court", firm=e.advertiser,
                )]
                n.google_search_url = (
                    f"https://www.google.com/search?q={quote(e.advertiser + ' ' + (e.dosya or ''))}"
                )

            results.append(n)
        except Exception:
            logger.exception("Turkey: error processing %s", e.notice_id)
        finally:
            mark_notice_processed(e.notice_id, e.title, e.published or "")

    if config.MIN_OPPORTUNITY_SCORE > 0:
        results = [r for r in results if r.opportunity_score >= config.MIN_OPPORTUNITY_SCORE]
    results.sort(key=lambda r: r.opportunity_score, reverse=True)
    logger.info("Turkey: %d opportunities ready", len(results))
    return results
