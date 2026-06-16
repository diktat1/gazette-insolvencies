#!/usr/bin/env python3
"""
Test the email lane routing in main.run_once.

The auction lane has been retired: only the insolvency lane is emailed, and
auction-tagged notices are dropped. Builds a handful of AnalysedNotice objects
with mixed .lane values, stubs the analysers and the SMTP-backed send_email (so
no network / SMTP is touched), runs run_once, and asserts:
  - send_email is invoked exactly once, for the insolvency lane
  - only non-auction notices reach the email
  - auction-tagged notices never appear in any send

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
    config.MALAYSIA_ENABLED = False

    main.analyse_notices = lambda lookback_days=None: list(monkeypatch_results)

    def fake_send(notices, lane="insolvency"):
        # Record (lane, [names]) and never touch SMTP.
        send_calls.append((lane, [n.company_name for n in notices]))
        return True

    main.send_email = fake_send


def test_auction_notices_dropped():
    results = [
        _notice("UK Corp Ltd", "insolvency"),
        _notice("RO BPI Co", "insolvency"),
        _notice("RO Auction Lot", "auction"),
        _notice("TR Icra Sale", "auction"),
        _notice("US Chapter 11 Co", "insolvency"),
    ]
    send_calls = []
    _install_stubs(results, send_calls)

    main.run_once(send=True)

    assert len(send_calls) == 1, f"expected 1 email, got {len(send_calls)}"
    lane, names = send_calls[0]
    assert lane == "insolvency", lane
    assert sorted(names) == sorted(
        ["UK Corp Ltd", "RO BPI Co", "US Chapter 11 Co"]
    ), names
    # Auction-tagged notices must never reach a send.
    assert "RO Auction Lot" not in names and "TR Icra Sale" not in names
    print("PASS: auction notices dropped, only insolvency lane emailed")


def test_no_email_when_only_auctions():
    results = [
        _notice("RO Auction Lot", "auction"),
        _notice("TR Icra Sale", "auction"),
    ]
    send_calls = []
    _install_stubs(results, send_calls)

    main.run_once(send=True)

    assert len(send_calls) == 0, f"expected 0 emails, got {len(send_calls)}"
    print("PASS: all-auction result sends no email")


def test_default_lane_is_insolvency():
    n = AnalysedNotice()
    assert n.lane == "insolvency", n.lane
    print("PASS: AnalysedNotice defaults to insolvency lane")


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    test_default_lane_is_insolvency()
    test_auction_notices_dropped()
    test_no_email_when_only_auctions()
    print("\nAll lane-routing tests passed.")
