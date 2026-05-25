"""Score a Turkish bankruptcy-law notice as an acquisition opportunity.

The portal gives a notice type (in the title/body) and, for asset-sale notices,
an estimated/auction price. We reward notices that point at a live, buyable
estate: an explicit asset sale (satış / artırma) or estimated price, a bankrupt
company estate (iflas masası), or a concordat (konkordato, where the business is
still a going concern). Maps to the shared HIGH/MEDIUM/LOW/SKIP buckets.
"""

import re


def _norm(s: str) -> str:
    # Lower-case and fold Turkish-specific characters so keyword matching is robust.
    s = (s or "").lower()
    return (s.replace("ı", "i").replace("İ".lower(), "i").replace("ş", "s")
             .replace("ğ", "g").replace("ü", "u").replace("ö", "o").replace("ç", "c"))


def classify_tr_lane(entry) -> str:
    """Return the report lane for a Turkish notice: "auction" or "insolvency".

    An icra / asset-sale notice (an explicit satis / artirma / ihale / icra, or a
    detail call that returned an estimated/auction price) is selling asset lots
    now, so it belongs in the auction lane. A bankruptcy-estate, concordat or
    liquidation case is a company-level insolvency event. Uses the same Turkish
    character folding as the scorer so the keyword match stays consistent.
    """
    blob = _norm(f"{entry.title} {entry.content} {entry.advertiser}")
    dosya = _norm(getattr(entry, "dosya", ""))
    has_price = bool(getattr(entry, "estimated_price", "") and re.search(r"\d", entry.estimated_price))
    has_auction_date = bool(getattr(entry, "auction_date", ""))
    is_sale = any(k in blob for k in ("satis", "artirma", "ihale", "icra")) or "icra" in dosya
    if has_price or has_auction_date or is_sale:
        return "auction"
    return "insolvency"


def score_tr(entry) -> dict:
    """Return {score, category, signals}."""
    signals: list[str] = []
    blob = _norm(f"{entry.title} {entry.content} {entry.advertiser}")
    dosya = _norm(entry.dosya)

    # The bankruptcy-law category mixes corporate bankruptcy with deceased-estate
    # liquidations ("tereke"). The latter are not acquisition targets, so cap them.
    is_estate = "tereke" in blob or "tereke" in dosya
    # Corporate markers: a bankruptcy office/commercial court, or an "İFLAS" case no.
    is_corporate = (
        "iflas dairesi" in blob or "iflas mudurlugu" in blob
        or "ticaret mahkemesi" in blob or "iflas" in dosya
    )

    if is_estate and not is_corporate:
        return {"score": 5, "category": "SKIP",
                "signals": ["Deceased-estate liquidation (tereke) - not a corporate target"]}

    score = 10  # baseline: a fresh bankruptcy-law notice
    has_price = False
    if entry.estimated_price and re.search(r"\d", entry.estimated_price):
        has_price = True
        score += 30
        signals.append(f"Asset sale with estimated value: {entry.estimated_price}")
    if entry.auction_date:
        score += 5
        signals.append(f"Auction date: {entry.auction_date}")

    if any(k in blob for k in ("satis", "artirma", "ihale")) and not has_price:
        score += 20
        signals.append("Asset sale / auction notice")
    if "iflas" in blob:
        score += 15
        signals.append("Bankruptcy estate (iflas)")
    if "konkordato" in blob:
        score += 15
        signals.append("Concordat (konkordato) - going concern under protection")
    if "tasfiye" in blob:
        score += 10
        signals.append("Liquidation (tasfiye)")

    # A commercial court / bankruptcy office confirms a corporate case.
    if is_corporate:
        score += 20
        signals.append("Corporate bankruptcy (commercial court / bankruptcy office / İFLAS case)")

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
