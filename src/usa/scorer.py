"""Score a US bankruptcy filing as an asset/business acquisition opportunity.

No free financials exist for US private debtors, so the score is built from the
signals the docket itself carries: the chapter (11 = going concern / a 363 sale,
7 = asset liquidation), whether the debtor is a company rather than an
individual, and whether it sits in a major commercial-bankruptcy court. Maps to
the same HIGH/MEDIUM/LOW/SKIP buckets as the UK/RO scorers.
"""

import re

# Suffixes that mark the debtor as a company rather than an individual.
COMPANY_TOKENS = re.compile(
    r"\b(LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|LP|L\.P|LLP|LTD|LIMITED|"
    r"CO|COMPANY|HOLDINGS?|GROUP|PARTNERS?|ENTERPRISES?|INDUSTRIES|SYSTEMS|"
    r"TECHNOLOGIES|REALTY|CAPITAL|VENTURES|PROPERTIES)\b",
    re.IGNORECASE,
)

# High-volume corporate-bankruptcy courts (Delaware, SDNY, S/N Texas, NJ, etc).
MAJOR_COURTS = {"deb", "nysb", "txsb", "txnb", "njb", "cacb", "ilnb", "flsb", "ganb", "vaeb"}


def is_company(name: str) -> bool:
    return bool(COMPANY_TOKENS.search(name or ""))


def score_us(entry) -> dict:
    """Return {score, category, signals}."""
    signals: list[str] = []
    name = entry.case_name or ""

    if not is_company(name):
        # Individual debtor (Chapter 7/11 personal) - nothing to acquire.
        return {"score": 0, "category": "SKIP",
                "signals": ["Individual debtor - no business/assets to acquire"]}

    score = 15  # baseline: a fresh corporate bankruptcy event
    signals.append("Corporate debtor")

    if entry.chapter == "11":
        score += 35
        signals.append("Chapter 11 - reorganisation / potential 363 asset sale (going concern)")
    elif entry.chapter == "7":
        score += 25
        signals.append("Chapter 7 - liquidation (trustee asset sale)")

    if entry.court_id in MAJOR_COURTS:
        score += 15
        signals.append(f"Major commercial-bankruptcy court ({entry.court_name})")

    # A docket with several named parties tends to be a substantive business case.
    if len(entry.parties) >= 3:
        score += 10
        signals.append(f"{len(entry.parties)} parties on the docket")

    score = min(score, 100)
    if score >= 60:
        category = "HIGH"
    elif score >= 40:
        category = "MEDIUM"
    elif score >= 20:
        category = "LOW"
    else:
        category = "SKIP"

    return {"score": score, "category": category, "signals": signals}
