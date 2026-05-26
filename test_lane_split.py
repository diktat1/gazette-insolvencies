#!/usr/bin/env python3
"""
Test the two-email lane split (insolvency vs auction) in main.run_once.

Builds a handful of AnalysedNotice objects with mixed .lane values, stubs the
analysers and the SMTP-backed send_email (so no network / SMTP is touched), runs
run_once, and asserts:
  - notices are partitioned correctly by .lane
  - send_email is invoked exactly twice (once per non-empty lane)
  - each call gets only the notices for its lane, with the right subject lane
  - an empty lane is skipped (never sends an empty email)

Run:
    python test_lane_split.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.email_report import AnalysedNotice
from src import config
import main


def _notice(name: str, lane: str, category: str = "HIGH") -> AnalysedNotice:
    n = AnalysedNotice()
    n.company_name = name
    n.lane = lane
    n.opportunity_category = category
    n.opportunity_score = 80
    n.notice_type = "test"
    return n


def _install_stubs(monkeypatch_results, send_calls):
    """Point run_once at canned results and capture send_email calls."""
    # Force email config to look present and country modules off.
    config.SMTP_USER = "test@example.com"
    config.EMAIL_TO = "to@example.com"
    config.ROMANIA_ENABLED = False
    config.USA_ENABLED = False
    config.TURKEY_ENABLED = False
    config.INDIA_ENABLED = False
    config.MALAYSIA_ENABLED = False

    main.analyse_notices = lambda lookback_days=None: list(monkeypatch_results)

    def fake_send(notices, lane="insolvency"):
        # Record (lane, [names]) and never touch SMTP.
        send_calls.append((lane, [n.company_name for n in notices]))
        return True

    main.send_email = fake_send


def test_mixed_lanes_sends_two_emails():
    results = [
        _notice("UK Corp Ltd", "insolvency"),
        _notice("RO BPI Co", "insolvency"),
        _notice("RO Auction Lot", "auction"),
        _notice("IN Liquidation Auction", "auction"),
        _notice("US Chapter 11 Co", "insolvency"),
    ]
    send_calls = []
    _install_stubs(results, send_calls)

    main.run_once(send=True)

    assert len(send_calls) == 2, f"expected 2 emails, got {len(send_calls)}"
    by_lane = {lane: names for lane, names in send_calls}
    assert set(by_lane) == {"insolvency", "auction"}, by_lane
    assert sorted(by_lane["insolvency"]) == sorted(
        ["UK Corp Ltd", "RO BPI Co", "US Chapter 11 Co"]
    ), by_lane["insolvency"]
    assert sorted(by_lane["auction"]) == sorted(
        ["RO Auction Lot", "IN Liquidation Auction"]
    ), by_lane["auction"]
    # No notice leaked across lanes.
    assert not (set(by_lane["insolvency"]) & set(by_lane["auction"]))
    print("PASS: mixed lanes -> two emails, correctly partitioned")


def test_empty_auction_lane_skipped():
    results = [
        _notice("UK Corp Ltd", "insolvency"),
        _notice("RO BPI Co", "insolvency"),
    ]
    send_calls = []
    _install_stubs(results, send_calls)

    main.run_once(send=True)

    assert len(send_calls) == 1, f"expected 1 email, got {len(send_calls)}"
    assert send_calls[0][0] == "insolvency"
    print("PASS: empty auction lane skipped (one email only)")


def test_default_lane_is_insolvency():
    n = AnalysedNotice()
    assert n.lane == "insolvency", n.lane
    print("PASS: AnalysedNotice defaults to insolvency lane")


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    test_default_lane_is_insolvency()
    test_mixed_lanes_sends_two_emails()
    test_empty_auction_lane_skipped()
    print("\nAll lane-split tests passed.")
