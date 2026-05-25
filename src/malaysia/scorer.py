"""Score a Malaysian winding-up notice as an acquisition opportunity.

The e-Insolvensi portal does not expose financials, so the score is built from
the notice type: a compulsory winding-up (court-ordered) puts a liquidator in
charge of realising the company's assets, the strongest buy signal; a members'
voluntary winding-up is a solvent wind-down with less to acquire. Maps to the
shared HIGH/MEDIUM/LOW/SKIP buckets.
"""


def score_my(entry) -> dict:
    """Return {score, category, signals}."""
    signals: list[str] = []
    t = (entry.notice_type or "").lower()

    score = 15  # baseline: a fresh winding-up event
    if "compulsory" in t or "court" in t:
        score += 40
        signals.append("Compulsory (court-ordered) winding-up - liquidator realising assets")
    elif "creditor" in t:
        score += 35
        signals.append("Creditors' voluntary winding-up - assets being realised")
    elif "member" in t or "voluntary" in t:
        score += 5
        signals.append("Members' voluntary winding-up (solvent) - limited asset opportunity")
    else:
        score += 20
        signals.append("Winding-up notice")

    score = min(score, 100)
    if score >= 55:
        category = "HIGH"
    elif score >= 35:
        category = "MEDIUM"
    elif score >= 20:
        category = "LOW"
    else:
        category = "SKIP"

    return {"score": score, "category": category, "signals": signals}
