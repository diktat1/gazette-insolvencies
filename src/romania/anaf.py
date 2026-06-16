"""Free ANAF (Romanian tax authority) enrichment by CUI.

Two public, keyless, live endpoints:
  - v9 VAT/status (batched, <=100 CUIs/call): name, address, phone, CAEN, active flag
  - /bilant?an=&cui=: annual financials (turnover, profit, employees, assets)

ANAF asks for max 1 request/second; we throttle accordingly.
"""

import logging
import os
import threading
import time
import unicodedata
from datetime import date

import requests

logger = logging.getLogger(__name__)

TVA_URL = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
BILANT_URL = "https://webservicesp.anaf.ro/bilant"
HEADERS = {"Content-Type": "application/json", "User-Agent": "insolvency-pipeline/1.0"}
TIMEOUT = 25
# Financials are the high-volume per-candidate call. A short timeout caps the
# tail cost of a slow/unresponsive ANAF response: a dead CUI tries 2 years, so
# its worst case is 2 * FIN_TIMEOUT rather than 3 * 25s.
FIN_TIMEOUT = 8

# Global financials rate limiter. Enrichment now runs across a thread pool, so
# the old per-call sleep no longer bounds the request rate. ANAF asks for max
# ~1 req/s; this gate enforces a minimum interval between financials requests
# across ALL worker threads. Tunable via ANAF_FIN_MIN_INTERVAL.
FIN_MIN_INTERVAL = float(os.getenv("ANAF_FIN_MIN_INTERVAL", "1.0"))
_fin_lock = threading.Lock()
_fin_next_at = [0.0]


def _fin_throttle() -> None:
    """Block until it is this thread's turn to issue a financials request,
    enforcing FIN_MIN_INTERVAL globally across threads."""
    with _fin_lock:
        now = time.monotonic()
        wait = _fin_next_at[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _fin_next_at[0] = now + FIN_MIN_INTERVAL


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def lookup_status_batch(cuis: list[str], qdate: str | None = None) -> dict[str, dict]:
    """Look up company status/name/address/CAEN for up to 100 CUIs at once."""
    qdate = qdate or date.today().isoformat()
    out: dict[str, dict] = {}
    for i in range(0, len(cuis), 100):
        chunk = cuis[i:i + 100]
        payload = [{"cui": int(c), "data": qdate} for c in chunk if str(c).isdigit()]
        if not payload:
            continue
        try:
            r = requests.post(TVA_URL, json=payload, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.warning("ANAF status batch failed: %s", exc)
            continue
        for item in data.get("found", []):
            g = item.get("date_generale", {}) or {}
            inact = item.get("stare_inactiv", {}) or {}
            cui = str(g.get("cui", ""))
            out[cui] = {
                "name": g.get("denumire", ""),
                "address": g.get("adresa", ""),
                "phone": g.get("telefon", ""),
                "caen": str(g.get("cod_CAEN", "") or ""),
                "reg_com": g.get("nrRegCom", ""),
                "registered": "INREGISTRAT" in (g.get("stare_inregistrare", "") or "").upper(),
                "inactive": bool(inact.get("statusInactivi", False)),
                "radiata": bool(inact.get("dataRadiere", "")),
            }
        time.sleep(1.1)
    return out


def get_financials(cui: str, years: list[int] | None = None) -> dict:
    """Latest available annual financials for a CUI. Returns {} if none."""
    if years is None:
        y = date.today().year
        years = [y - 1, y - 2]
    for yr in years:
        try:
            _fin_throttle()
            r = requests.get(BILANT_URL, params={"an": yr, "cui": cui}, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=FIN_TIMEOUT)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        inds = data.get("i") or []
        if not inds:
            continue
        fin = {"year": data.get("an", yr), "caen": str(data.get("caen", "") or ""),
               "caen_desc": data.get("den_caen", ""), "turnover": None, "profit": None,
               "employees": None, "total_assets": None}
        imob = circ = 0.0
        for ind in inds:
            label = _norm(ind.get("val_den_indicator", ""))
            val = ind.get("val_indicator")
            if val is None:
                continue
            if "cifra de afaceri" in label:
                fin["turnover"] = val
            elif "profit net" in label:
                fin["profit"] = val
            elif "pierdere neta" in label and fin.get("profit") is None:
                fin["profit"] = -val
            elif "numar mediu de salariati" in label or "numar mediu salariati" in label:
                fin["employees"] = val
            elif label.startswith("active imobilizate"):
                imob = val
            elif label.startswith("active circulante"):
                circ = val
        if imob or circ:
            fin["total_assets"] = imob + circ
        return fin
    return {}
