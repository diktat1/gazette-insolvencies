"""Orchestrate the Romanian pipeline into AnalysedNotice objects.

fetch (lege5) -> enrich (ANAF status + financials) -> score -> emit
AnalysedNotice, so RO opportunities drop straight into the daily UK email.
"""

import logging
from typing import Optional

from src import config
from src.email_report import AnalysedNotice
from src.db import is_notice_processed, mark_notice_processed
from src.romania.feed import fetch_ro_entries
from src.romania.anaf import lookup_status_batch, get_financials
from src.romania.scorer import score_ro

logger = logging.getLogger(__name__)


def _fmt_ron(v) -> str:
    if not v:
        return ""
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"RON {v/1_000_000:.1f}m"
    if abs(v) >= 1_000:
        return f"RON {v/1_000:.0f}k"
    return f"RON {v:,.0f}"


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

    status = lookup_status_batch([e.cui for e in fresh])

    results: list[AnalysedNotice] = []
    for e in fresh:
        try:
            st = status.get(e.cui, {})
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

            results.append(n)
        except Exception:
            logger.exception("Romania: error processing CUI %s", e.cui)
        finally:
            mark_notice_processed(e.notice_id, e.company_name, e.published or "")

    if config.MIN_OPPORTUNITY_SCORE > 0:
        results = [r for r in results if r.opportunity_score >= config.MIN_OPPORTUNITY_SCORE]
    results.sort(key=lambda r: r.opportunity_score, reverse=True)
    logger.info("Romania: %d opportunities ready", len(results))
    return results
