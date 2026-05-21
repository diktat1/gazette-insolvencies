"""Score a Romanian insolvency opportunity from ANAF enrichment.

Mirrors the spirit of the UK opportunity_scorer: reward balance-sheet
substance, trading history, headcount and asset-rich sectors; penalise dead
shells and struck-off entities. Maps to the same HIGH/MEDIUM/LOW/SKIP buckets.
"""

# CAEN (≈ NACE) divisions where insolvency tends to leave tangible/saleable assets
ASSET_RICH_DIVISIONS = {
    "01", "02", "03",                                              # agriculture
    "05", "06", "07", "08", "09",                                  # mining
    *(f"{d:02d}" for d in range(10, 34)),                          # manufacturing 10-33
    "41", "42", "43",                                              # construction
    "45", "46", "47",                                              # wholesale/retail
    "49", "50", "51", "52", "53",                                  # transport/storage
    "55", "56",                                                    # accommodation/food
    "68",                                                          # real estate
}

ASSET_HINTS = {
    "agriculture": "Land, livestock, machinery, stock",
    "mining": "Plant, equipment, mineral rights",
    "manufacturing": "Plant & machinery, stock, equipment",
    "construction": "Plant, vehicles, work-in-progress, receivables",
    "trade": "Stock, inventory, fittings",
    "transport": "Vehicles, fleet, depots",
    "hospitality": "Fixtures, equipment, premises",
    "real estate": "Property, land",
}


def _division(caen: str) -> str:
    return (caen or "")[:2]


def _sector_bucket(caen: str) -> str:
    d = _division(caen)
    if d in {"01", "02", "03"}: return "agriculture"
    if d in {"05", "06", "07", "08", "09"}: return "mining"
    if d.isdigit() and 10 <= int(d) <= 33: return "manufacturing"
    if d in {"41", "42", "43"}: return "construction"
    if d in {"45", "46", "47"}: return "trade"
    if d in {"49", "50", "51", "52", "53"}: return "transport"
    if d in {"55", "56"}: return "hospitality"
    if d == "68": return "real estate"
    return ""


def score_ro(status: dict, fin: dict) -> dict:
    """Return {score, category, signals, asset_hint}."""
    signals: list[str] = []
    caen = (fin.get("caen") or status.get("caen") or "")

    # Struck off / radiata = nothing to buy
    if status.get("radiata"):
        return {"score": 0, "category": "SKIP", "signals": ["Struck off the register"], "asset_hint": ""}

    score = 10  # baseline: a fresh insolvency event
    ta = fin.get("total_assets") or 0
    to = fin.get("turnover") or 0
    emp = fin.get("employees") or 0

    if ta > 10_000:
        score += 25
        signals.append(f"Balance-sheet assets RON {ta:,.0f} ({fin.get('year','')})")
    if ta > 1_000_000:
        score += 15
    if to > 0:
        score += 15
        signals.append(f"Trading history - turnover RON {to:,.0f} ({fin.get('year','')})")
    if to > 1_000_000:
        score += 10
    if emp >= 5:
        score += 10
        signals.append(f"{emp} employees")

    bucket = _sector_bucket(caen)
    if _division(caen) in ASSET_RICH_DIVISIONS:
        score += 15
        signals.append(f"Asset-rich sector: {fin.get('caen_desc') or bucket or 'CAEN ' + caen}")

    if status.get("registered") and not status.get("inactive"):
        score += 5
        signals.append("Active / registered at tax authority")
    elif status.get("inactive"):
        signals.append("Flagged inactive at tax authority")

    score = min(score, 100)
    if score >= 70:
        category = "HIGH"
    elif score >= 45:
        category = "MEDIUM"
    elif score >= 25:
        category = "LOW"
    else:
        category = "SKIP"

    return {"score": score, "category": category, "signals": signals,
            "asset_hint": ASSET_HINTS.get(bucket, "")}
