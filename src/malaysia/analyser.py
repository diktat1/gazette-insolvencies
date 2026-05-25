"""Orchestrate the Malaysian pipeline into AnalysedNotice objects.

fetch (e-Insolvensi, credential-gated) -> score -> emit AnalysedNotice. Wired
OFF by default; fails closed so it can never break the UK/RO report.
"""

import logging
from typing import Optional

from src import config
from src.email_report import AnalysedNotice
from src.db import is_notice_processed, mark_notice_processed
from src.malaysia.feed import fetch_my_entries
from src.malaysia.scorer import score_my

logger = logging.getLogger(__name__)


def analyse_malaysia_notices(lookback_days: Optional[int] = None) -> list[AnalysedNotice]:
    lookback = lookback_days if lookback_days is not None else config.MALAYSIA_LOOKBACK_DAYS
    entries = fetch_my_entries(lookback_days=lookback, max_companies=config.MALAYSIA_MAX_COMPANIES)
    logger.info("Malaysia: fetched %d winding-up notices (lookback=%d days)", len(entries), lookback)

    fresh = [e for e in entries if not is_notice_processed(e.notice_id)]
    if not fresh:
        return []

    results: list[AnalysedNotice] = []
    for e in fresh:
        try:
            assessment = score_my(e)

            n = AnalysedNotice()
            n.country = "MY"
            n.notice_id = e.notice_id
            n.notice_url = e.detail_url
            n.notice_type = e.notice_type or "Winding-up (Malaysia)"
            n.published_date = e.notice_date
            n.company_name = "🇲🇾 " + e.company_name
            n.company_number = e.company_number
            n.court_name = e.court
            n.ch_url = e.detail_url
            n.ch_status = "Winding-up"

            n.opportunity_score = assessment["score"]
            n.opportunity_category = assessment["category"]
            n.opportunity_signals = assessment["signals"]

            results.append(n)
        except Exception:
            logger.exception("Malaysia: error processing %s", e.notice_id)
        finally:
            mark_notice_processed(e.notice_id, e.company_name, e.notice_date or "")

    if config.MIN_OPPORTUNITY_SCORE > 0:
        results = [r for r in results if r.opportunity_score >= config.MIN_OPPORTUNITY_SCORE]
    results.sort(key=lambda r: r.opportunity_score, reverse=True)
    logger.info("Malaysia: %d opportunities ready", len(results))
    return results
