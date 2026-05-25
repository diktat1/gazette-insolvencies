"""Free Turkish insolvency-notice feed via ilan.gov.tr (Basın İlan Kurumu).

ilan.gov.tr is the official state announcements portal. Its public Angular front
end is backed by an unauthenticated ABP JSON API. The `Ad/AdsByFilter` endpoint
returns ads newest-first (paged via skipCount); we walk recent pages and keep
only the bankruptcy-law category, which every ad encodes in its slug prefix
`iflas-hukuku-davalari-` ("İflas Hukuku Davaları" = bankruptcy law cases:
liquidations, creditor-meeting calls, concordat). The advertiser is the
bankruptcy office / commercial court; the case (dosya) number and debtor detail
come from the per-ad detail call.
"""

import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

import certifi
import requests

from src import config

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _ca_bundle() -> str:
    """Path to a CA bundle that can verify ilan.gov.tr.

    The server sends only its leaf certificate and omits the "GeoTrust TLS RSA
    CA G1" intermediate, so certifi alone cannot build the chain to the DigiCert
    root. We vendor that intermediate and append it to certifi's bundle, keeping
    TLS verification ON (no insecure verify=False).
    """
    inter = os.path.join(os.path.dirname(__file__), "ca", "geotrust_tls_rsa_ca_g1.pem")
    if not os.path.exists(inter):
        return certifi.where()
    out = os.path.join(tempfile.gettempdir(), "ilan_gov_tr_ca_bundle.pem")
    try:
        if not os.path.exists(out):
            with open(out, "w", encoding="utf-8") as f:
                f.write(open(certifi.where(), encoding="utf-8").read())
                f.write("\n")
                f.write(open(inter, encoding="utf-8").read())
        return out
    except Exception:
        return certifi.where()


API_BASE = "https://www.ilan.gov.tr/api/api/services/app"
FILTER_URL = f"{API_BASE}/Ad/AdsByFilter"
DETAIL_URL = f"{API_BASE}/AdDetail/GetAdDetail"
WEB_BASE = "https://www.ilan.gov.tr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Content-Type": "application/json-patch+json",
    "Accept": "text/plain",
}
# Bankruptcy-law category: every ad's slug starts with this when it belongs to
# "İflas Hukuku Davaları" (setId 66 / taxId 12 in the portal taxonomy).
BANKRUPTCY_SLUG_PREFIX = "iflas-hukuku-davalari"


@dataclass
class TrEntry:
    ad_id: str
    ad_no: str
    title: str
    advertiser: str        # bankruptcy office / court
    city: str
    county: str
    published: str         # ISO date or ""
    url: str
    dosya: str = ""        # court case number
    debtor: str = ""       # filled by enrichment
    content: str = ""      # full notice body (enrichment)
    estimated_price: str = ""
    auction_date: str = ""

    @property
    def notice_id(self) -> str:
        return f"TR-{self.ad_no or self.ad_id}"


def _parse_date(s: str) -> str:
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return s[:10]


def fetch_tr_entries(lookback_days: int = 1, max_companies: int = 60) -> list[TrEntry]:
    """Collect recent bankruptcy-law notices, newest first.

    Pages AdsByFilter (default ordering is newest-first) and keeps the
    bankruptcy-category ads until the publication date falls outside
    `lookback_days` or `max_companies` is reached.
    """
    cutoff = date.today() - timedelta(days=max(1, lookback_days))
    page_size = 20
    out: list[TrEntry] = []
    seen: set[str] = set()

    for page in range(config.TURKEY_MAX_PAGES):
        body = {"searchText": "", "skipCount": page * page_size, "maxResultCount": page_size}
        try:
            resp = requests.post(FILTER_URL, json=body, headers=HEADERS,
                                 timeout=config.REQUEST_TIMEOUT, verify=_ca_bundle())
            resp.raise_for_status()
            ads = (resp.json().get("result") or {}).get("ads") or []
        except Exception as exc:
            logger.warning("ilan.gov.tr page %d failed: %s", page, exc)
            break
        if not ads:
            break

        stop = False
        for a in ads:
            pub = _parse_date(a.get("publishStartDate") or "")
            if pub and pub < cutoff.isoformat():
                stop = True
                break
            slug = a.get("slugifyTitle") or ""
            if not slug.startswith(BANKRUPTCY_SLUG_PREFIX):
                continue
            ad_no = str(a.get("adNo") or "")
            ad_id = str(a.get("id") or "")
            if (ad_no or ad_id) in seen:
                continue
            seen.add(ad_no or ad_id)

            dosya = ""
            for f in (a.get("adTypeFilters") or []):
                if "dosya" in (f.get("key") or "").lower():
                    dosya = f.get("value") or ""

            url_str = a.get("urlStr") or ""
            out.append(TrEntry(
                ad_id=ad_id, ad_no=ad_no,
                title=(a.get("title") or "").strip(),
                advertiser=(a.get("advertiserName") or "").strip(),
                city=a.get("addressCityName") or "",
                county=a.get("addressCountyName") or "",
                published=pub,
                url=(WEB_BASE + url_str) if url_str else "",
                dosya=dosya,
            ))
            if len(out) >= max_companies:
                return out

        if stop:
            break
        time.sleep(config.TURKEY_REQUEST_PAUSE)

    return out


def enrich_tr_entry(entry: TrEntry) -> None:
    """Pull the full notice detail (debtor, body text, asset/auction price)."""
    if not entry.ad_id:
        return
    try:
        resp = requests.get(DETAIL_URL, params={"id": entry.ad_id},
                            headers={"User-Agent": HEADERS["User-Agent"]},
                            timeout=config.REQUEST_TIMEOUT, verify=_ca_bundle())
        resp.raise_for_status()
        d = resp.json().get("result") or {}
    except Exception as exc:
        logger.debug("TR detail %s failed: %s", entry.ad_id, exc)
        return

    import re
    content_html = d.get("content") or ""
    text = re.sub(r"<[^>]+>", " ", content_html).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    entry.content = text[:600]

    # Pull the bankrupt company ("müflis") name where the notice states it. The
    # debtor runs from the "müflis" label up to and including the company-type
    # suffix (ANONİM/LİMİTED ŞİRKETİ, ŞTİ, A.Ş.). Best-effort: blank if not found.
    m = re.search(
        r"m[üu]flis(?:in)?[^:]{0,60}:\s*([0-9A-Za-zÇĞİÖŞÜçğıöşü .,&'\-]{3,90}?"
        r"(?:ANON[İI]M [ŞS][İI]RKET[İI]|L[İI]M[İI]TED [ŞS][İI]RKET[İI]|[ŞS][İI]RKET[İI]|[ŞS]T[İI]\.?|A\.[ŞS]\.))",
        text, re.IGNORECASE)
    if m:
        entry.debtor = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")
    entry.estimated_price = str(d.get("attrEstimatedPrice") or d.get("attrPrice") or "").strip()
    entry.auction_date = _parse_date(d.get("attrAuctionDate") or "")
    if not entry.dosya:
        for f in (d.get("adTypeFilters") or []):
            if "dosya" in (f.get("key") or "").lower():
                entry.dosya = f.get("value") or ""
