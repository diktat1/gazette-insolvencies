"""Score an IBBI public announcement as an acquisition opportunity.

The most useful signal is the announcement type. Liquidation and auction notices
point at assets being sold now; a CIRP (Corporate Insolvency Resolution Process)
invites resolution plans for a going concern - both are relevant to an acquirer.
Voluntary liquidation of a solvent company is usually a wind-down with little to
buy, so it scores low. Maps to the shared HIGH/MEDIUM/LOW/SKIP buckets.
"""


def score_in(entry) -> dict:
    """Return {score, category, signals}."""
    signals: list[str] = []
    t = (entry.pa_type or "").lower()

    score = 10  # baseline: a fresh public announcement
    if "auction" in t:
        score += 45
        signals.append("Liquidation auction - assets being sold now")
    elif "voluntary" in t:
        score += 5
        signals.append("Voluntary liquidation (solvent wind-down) - limited asset opportunity")
    elif "liquidation" in t:
        score += 40
        signals.append("Liquidation process - liquidator realising assets")
    elif "insolvency resolution" in t or "cirp" in t:
        score += 35
        signals.append("CIRP - resolution plans invited for a going concern")

    if entry.ip_name:
        score += 10
        signals.append(f"Insolvency professional named: {entry.ip_name}")
    if entry.submission_deadline:
        signals.append(f"Submission deadline: {entry.submission_deadline}")

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
