"""Local practitioner -> contact directory (free, self-healing).

ECRIS gives the practitioner FIRM per case; this resolves the firm to a
contact. Backed by a small committed JSON seed of the major firms, which grows
over time via add_contact() as new firms are verified. Romanian insolvency is
concentrated in a few dozen firms, so the directory asymptotes quickly.
"""

import json
import os
import re
import unicodedata

DIR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "ro_practitioner_contacts.json",
)


def _key(firm: str) -> str:
    s = unicodedata.normalize("NFKD", firm or "").encode("ascii", "ignore").decode().upper()
    s = re.sub(r"\b(SPRL|IPURL|SCP|SPRL FILIALA|FILIALA|SC|SA|SRL)\b", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load() -> dict:
    try:
        with open(DIR_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def resolve_contact(firm: str) -> dict:
    """Return {email, phone, website} for a practitioner firm, or {}."""
    if not firm:
        return {}
    directory = _load()
    k = _key(firm)
    if not k:
        return {}
    for name, info in directory.items():
        if _key(name) == k:
            return info
    for name, info in directory.items():
        nk = _key(name)
        if nk and (nk in k or k in nk):
            return info
    return {}


def add_contact(firm: str, email: str = "", phone: str = "", website: str = "") -> None:
    """Add/update a firm's contact (self-healing cache)."""
    directory = _load()
    directory[firm] = {"email": email, "phone": phone, "website": website}
    os.makedirs(os.path.dirname(DIR_PATH), exist_ok=True)
    with open(DIR_PATH, "w", encoding="utf-8") as f:
        json.dump(directory, f, ensure_ascii=False, indent=2, sort_keys=True)
