"""Orchestrate the Romanian pipeline into AnalysedNotice objects.

fetch (lege5) -> enrich (ANAF status + financials) -> score -> emit
AnalysedNotice, so RO opportunities drop straight into the daily UK email.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from src import config
from src.email_report import AnalysedNotice
from src.db import is_notice_processed, mark_notice_processed
from src.romania.feed import fetch_ro_entries
from src.romania.anaf import lookup_status_batch, get_financials
from src.romania.scorer import score_ro
from src.romania.ecris import lookup_practitioner
from src.romania.contacts import resolve_contact

logger = logging.getLogger(__name__)


@dataclass
class ROPractitioner:
    name: str = ""
    role: str = ""
    firm: str = ""
    email: str = ""
    phone: str = ""


def _fmt_ron(v) -> str:
    if not v:
        return ""
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"RON {v/1_000_000:.1f}m"
    if abs(v) >= 1_000:
        return f"RON {v/1_000:.0f}k"
    return f"RON {v:,.0f}"


def _enrich_one(e, st: dict) -> Optional[AnalysedNotice]:
    """Deep-enrich a single Romanian candidate: ANAF financials + score, plus an
    ECRIS practitioner lookup for the cases worth pursuing. Returns the built
    AnalysedNotice. Runs in a worker thread, so it must not touch shared state."""
    fin = get_financials(e.cui)
    assessment = score_ro(st, fin)

    n = AnalysedNotice()
    n.country = "RO"
    n.notice_id = e.notice_id
    n.notice_url = ""
    n.notice_type = "Insolvency proceedings (Romania)"
    n.published_date = e.published
    n.company_name = "🇷🇴 " + (st.get("name") or e.company_name)
    n.company_number = f"CUI {e.cui}" + (f" · {st.get('reg_com')}" if st.get("reg_com") else "")
    n.registered_address = st.get("address", "")
    n.court_case_number = e.dosar
    n.ch_url = e.detail_url

    # status line
    if st.get("radiata"):
        n.ch_status = "Struck off"
    elif st.get("inactive"):
        n.ch_status = "Inactive (tax authority)"
    elif st.get("registered"):
        n.ch_status = "Active / registered"
    if fin.get("year"):
        n.ch_accounts_type = f"{fin['year']} financials filed"

    # sector + financials
    n.sector = fin.get("caen_desc") or (f"CAEN {fin.get('caen')}" if fin.get("caen") else "")
    n.sector_code = fin.get("caen") or st.get("caen", "")
    n.turnover = _fmt_ron(fin.get("turnover"))
    n.total_assets = _fmt_ron(fin.get("total_assets"))
    n.employees = str(fin.get("employees")) if fin.get("employees") else ""

    if assessment["asset_hint"]:
        n.estimated_assets = [assessment["asset_hint"]]

    n.opportunity_score = assessment["score"]
    n.opportunity_category = assessment["category"]
    n.opportunity_signals = assessment["signals"]

    # Practitioner lookup (free, via ECRIS) for the cases worth pursuing.
    # ECRIS lists the administrator/lichidator judiciar only for some
    # courts, so this is best-effort; contact resolved from the local
    # directory where the firm is known.
    if assessment["category"] in ("HIGH", "MEDIUM"):
        prac = lookup_practitioner(e.dosar, company_name=st.get("name") or e.company_name)
        # Court-confirmed debtor status screens out creditor false positives.
        if prac.get("is_debtor") is True:
            n.opportunity_signals = ["Debtor in its own insolvency (court-confirmed)"] + n.opportunity_signals
        elif prac.get("is_debtor") is False:
            n.opportunity_signals = n.opportunity_signals + ["Note: appears only as a creditor in this case - verify it is itself insolvent"]
        if prac.get("firm"):
            contact = resolve_contact(prac["firm"])
            role = prac.get("role", "Insolvency practitioner")
            if prac.get("source") == "soluţie":
                role += " (named in court decision)"
            n.practitioners = [ROPractitioner(
                name=prac["firm"], role=role,
                firm=prac["firm"], email=contact.get("email", ""),
                phone=contact.get("phone", ""),
            )]
            n.google_search_url = (
                contact.get("website")
                or f"https://www.google.com/search?q={quote(prac['firm'] + ' insolventa contact')}"
            )
            if contact.get("email"):
                n.ip_email = contact["email"]
                n.draft_email_subject = f"Expression of interest - {st.get('name') or e.company_name}"

    return n


def analyse_romania_notices(lookback_days: Optional[int] = None) -> list[AnalysedNotice]:
    lookback = lookback_days if lookback_days is not None else config.ROMANIA_LOOKBACK_DAYS
    max_co = config.ROMANIA_MAX_COMPANIES

    entries = fetch_ro_entries(lookback_days=lookback, max_companies=max_co)
    logger.info("Romania: fetched %d BPI entries (lookback=%d days)", len(entries), lookback)

    # Dedup against tracker so the daily run never repeats backfilled cases.
    fresh = [e for e in entries if not is_notice_processed(e.notice_id)]
    logger.info("Romania: %d new after dedup", len(fresh))
    if not fresh:
        return []

    # Batched status for ALL of the day's entries (fast: 100 CUIs/call). Romania
    # publishes ~600 insolvency notices/day; financial lookups are 1 req/s so we
    # can't deep-enrich every one. Pre-filter on the (cheap) status CAEN to the
    # asset-rich, live companies, then deep-enrich only those (capped).
    from src.romania.scorer import is_asset_rich_caen
    status = lookup_status_batch([e.cui for e in fresh])

    candidates, skipped = [], []
    for e in fresh:
        s = status.get(e.cui) or {}
        if s and not s.get("radiata") and is_asset_rich_caen(s.get("caen", "")):
            candidates.append(e)
        else:
            skipped.append(e)
    for e in skipped:
        mark_notice_processed(e.notice_id, e.company_name, e.published or "")

    # Hard cap on deep-enriched candidates. ANAF financials are ~1 req/s, so an
    # uncapped serial enrich of 1000+ candidates blows past the GitHub 6h job cap
    # (see ROMANIA_MAX_ENRICH / ROMANIA_TIME_BUDGET_S in config). Candidates over
    # the cap are dropped (marked processed) rather than carried over, so they
    # never accumulate into an unbounded backlog.
    max_enrich = config.ROMANIA_MAX_ENRICH
    if max_enrich and len(candidates) > max_enrich:
        overflow = candidates[max_enrich:]
        candidates = candidates[:max_enrich]
        logger.warning("Romania: capping enrichment at %d candidates; dropping %d over the cap",
                       max_enrich, len(overflow))
        for e in overflow:
            mark_notice_processed(e.notice_id, e.company_name, e.published or "")

    budget_s = config.ROMANIA_TIME_BUDGET_S
    workers = config.ROMANIA_CONCURRENCY
    logger.info("Romania: enriching %d asset-rich live candidates of %d entries "
                "(budget %ds, %d workers)", len(candidates), len(fresh), budget_s, workers)

    # Parallel enrichment. Each candidate's work is network-latency-bound (ANAF
    # financials + an ECRIS practitioner lookup with a long timeout), so a serial
    # loop wastes most of the budget waiting. We fan out across a thread pool;
    # ANAF financials are still globally throttled to ~1 req/s inside the client,
    # but the slow ECRIS calls and financials timeouts now overlap instead of
    # blocking each other. The wall-clock budget still bounds the whole stage.
    results: list[AnalysedNotice] = []
    deadline = (time.monotonic() + budget_s) if budget_s else None
    ex = ThreadPoolExecutor(max_workers=workers)
    fut_map = {ex.submit(_enrich_one, e, status.get(e.cui, {})): e for e in candidates}
    done = 0
    try:
        for fut in as_completed(fut_map):
            e = fut_map[fut]
            try:
                n = fut.result()
                if n is not None:
                    results.append(n)
            except Exception:
                logger.exception("Romania: error processing CUI %s", e.cui)
            done += 1
            if deadline and time.monotonic() > deadline:
                logger.warning("Romania: time budget (%ds) reached after %d/%d candidates; "
                               "dropping the rest", budget_s, done, len(candidates))
                break
    finally:
        # Drop anything not yet started; running calls finish in the background.
        ex.shutdown(wait=False, cancel_futures=True)

    # Mark every candidate processed (INSERT OR IGNORE is idempotent), so neither
    # the enriched ones nor any budget-dropped ones return tomorrow as a backlog.
    for e in candidates:
        mark_notice_processed(e.notice_id, e.company_name, e.published or "")

    # Asset-sale feed: live auctions with practitioner contact on the page.
    if config.ROMANIA_AUCTIONS_ENABLED:
        try:
            from src.romania.auctions import fetch_auction_opportunities
            auctions = fetch_auction_opportunities(max_listings=config.ROMANIA_AUCTIONS_MAX)
            results.extend(auctions)
        except Exception:
            logger.exception("Romania auctions feed failed; continuing")

    if config.MIN_OPPORTUNITY_SCORE > 0:
        results = [r for r in results if r.opportunity_score >= config.MIN_OPPORTUNITY_SCORE]
    results.sort(key=lambda r: r.opportunity_score, reverse=True)
    logger.info("Romania: %d opportunities ready", len(results))
    return results
