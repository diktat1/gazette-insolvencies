"""Orchestrate the US pipeline into AnalysedNotice objects.

fetch (CourtListener) -> score -> emit AnalysedNotice, so US opportunities drop
straight into the same daily email report as the UK/RO ones.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from src import config
from src.email_report import AnalysedNotice
from src.db import is_notice_processed, mark_notice_processed
from src.usa.feed import fetch_us_entries
from src.usa.scorer import score_us

logger = logging.getLogger(__name__)

CHAPTER_LABEL = {
    "7": "Chapter 7 bankruptcy (liquidation)",
    "11": "Chapter 11 bankruptcy (reorganisation)",
}


@dataclass
class UsPractitioner:
    name: str = ""
    role: str = ""
    firm: str = ""
    email: str = ""
    phone: str = ""


def analyse_usa_notices(lookback_days: Optional[int] = None) -> list[AnalysedNotice]:
    lookback = lookback_days if lookback_days is not None else config.USA_LOOKBACK_DAYS
    entries = fetch_us_entries(lookback_days=lookback, max_companies=config.USA_MAX_COMPANIES)
    logger.info("USA: fetched %d bankruptcy filings (lookback=%d days)", len(entries), lookback)

    fresh = [e for e in entries if not is_notice_processed(e.notice_id)]
    logger.info("USA: %d new after dedup", len(fresh))
    if not fresh:
        return []

    results: list[AnalysedNotice] = []
    for e in fresh:
        try:
            assessment = score_us(e)

            n = AnalysedNotice()
            n.country = "US"
            n.notice_id = e.notice_id
            n.notice_url = e.docket_url
            n.notice_type = CHAPTER_LABEL.get(e.chapter, f"Chapter {e.chapter} bankruptcy")
            n.published_date = e.date_filed
            n.company_name = "🇺🇸 " + e.case_name
            n.company_number = f"Case {e.docket_number}"
            n.court_name = e.court_name
            n.court_case_number = e.docket_number
            n.ch_url = e.docket_url
            n.ch_status = "In bankruptcy"

            n.opportunity_score = assessment["score"]
            n.opportunity_category = assessment["category"]
            n.opportunity_signals = assessment["signals"]

            results.append(n)
        except Exception:
            logger.exception("USA: error processing %s", e.notice_id)
        finally:
            mark_notice_processed(e.notice_id, e.case_name, e.date_filed or "")

    if config.MIN_OPPORTUNITY_SCORE > 0:
        results = [r for r in results if r.opportunity_score >= config.MIN_OPPORTUNITY_SCORE]
    results.sort(key=lambda r: r.opportunity_score, reverse=True)
    logger.info("USA: %d opportunities ready", len(results))
    return results
