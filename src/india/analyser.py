"""Orchestrate the Indian pipeline into AnalysedNotice objects.

fetch (IBBI register) -> score -> emit AnalysedNotice, so IN opportunities drop
straight into the same daily email report. The insolvency professional named in
each announcement is surfaced as the contact route.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from src import config
from src.email_report import AnalysedNotice
from src.db import is_notice_processed, mark_notice_processed
from src.india.feed import fetch_in_entries
from src.india.scorer import score_in

logger = logging.getLogger(__name__)


@dataclass
class InPractitioner:
    name: str = ""
    role: str = ""
    firm: str = ""
    email: str = ""
    phone: str = ""


def analyse_india_notices(lookback_days: Optional[int] = None) -> list[AnalysedNotice]:
    lookback = lookback_days if lookback_days is not None else config.INDIA_LOOKBACK_DAYS
    entries = fetch_in_entries(lookback_days=lookback, max_companies=config.INDIA_MAX_COMPANIES)
    logger.info("India: fetched %d IBBI announcements (lookback=%d days)", len(entries), lookback)

    fresh = [e for e in entries if not is_notice_processed(e.notice_id)]
    logger.info("India: %d new after dedup", len(fresh))
    if not fresh:
        return []

    results: list[AnalysedNotice] = []
    for e in fresh:
        try:
            assessment = score_in(e)

            n = AnalysedNotice()
            n.country = "IN"
            n.notice_id = e.notice_id
            n.notice_url = e.pdf_url
            n.notice_type = e.pa_type
            n.published_date = e.announce_date
            n.company_name = "🇮🇳 " + e.debtor
            n.registered_address = e.address
            n.ch_url = e.pdf_url or "https://ibbi.gov.in/public-announcement"
            n.ch_status = "Insolvency / liquidation process"

            n.opportunity_score = assessment["score"]
            n.opportunity_category = assessment["category"]
            n.opportunity_signals = assessment["signals"]

            if e.ip_name:
                n.practitioners = [InPractitioner(
                    name=e.ip_name, role="Insolvency professional", firm=e.ip_name,
                )]
                n.google_search_url = (
                    f"https://www.google.com/search?q={quote(e.ip_name + ' insolvency professional India contact')}"
                )

            results.append(n)
        except Exception:
            logger.exception("India: error processing %s", e.notice_id)
        finally:
            mark_notice_processed(e.notice_id, e.debtor, e.announce_date or "")

    if config.MIN_OPPORTUNITY_SCORE > 0:
        results = [r for r in results if r.opportunity_score >= config.MIN_OPPORTUNITY_SCORE]
    results.sort(key=lambda r: r.opportunity_score, reverse=True)
    logger.info("India: %d opportunities ready", len(results))
    return results
