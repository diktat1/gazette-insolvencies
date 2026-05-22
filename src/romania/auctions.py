"""Romanian insolvency asset-sale feed via licitatii-insolventa.ro (UNPIR).

Unlike the BPI feed (insolvency *events*), this lists assets actively *for
sale*, and because the selling practitioner is the advertiser, each detail page
carries their contact directly - so these cards get a full IP email + draft,
not just a name. Free, server-rendered HTML.
"""

import logging
import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from src import config
from src.email_report import AnalysedNotice

logger = logging.getLogger(__name__)

BASE = "https://www.licitatii-insolventa.ro"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ro,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
# asset-rich categories worth surfacing (skip auto/altele noise)
CATEGORIES = {
    "industrial": ("Industrial", 25),
    "imobiliare": ("Real estate", 25),
    "afaceri": ("Business / going concern", 25),
    "office": ("Office", 10),
}
DETAIL_RE = re.compile(r'href="(https://www\.licitatii-insolventa\.ro/[a-z][^"]*_i\d+)"')
FIRM_RE = re.compile(r"Subscrisa\s+([A-ZĂÂÎŞŢ0-9][^,\.]{2,70}?(?:SPRL|IPURL))", re.I)
COMPANY_RE = re.compile(r"([A-ZĂÂÎŞŢ0-9][A-ZĂÂÎŞŢ0-9 &'\.\-]{2,60}?(?:S\.?R\.?L|S\.?A|S\.?C\.?S)\.?)\b")
EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")
PRICE_RE = re.compile(r"(\d[\d.\s]{2,}(?:,\d+)?)\s*(lei|ron|eur|euro|€)", re.I)


@dataclass
class AucPractitioner:
    name: str = ""
    role: str = ""
    firm: str = ""
    email: str = ""
    phone: str = ""


def _listing_id(url: str) -> str:
    m = re.search(r"_i(\d+)", url)
    return m.group(1) if m else url


def fetch_auction_opportunities(max_listings: int = 30) -> list[AnalysedNotice]:
    # Use a session and warm it on the homepage first so Cloudflare issues its
    # clearance cookies; bare requests from datacenter IPs (GitHub runners) get
    # challenged and return non-200.
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        sess.get(BASE, timeout=25)
        time.sleep(1.0)
    except Exception as exc:
        logger.warning("auctions: homepage warm-up failed: %s", exc)

    # 1. collect recent detail URLs across asset-rich categories
    detail_urls: list[str] = []
    seen: set[str] = set()
    per_cat = max(4, max_listings // len(CATEGORIES))
    for cat in CATEGORIES:
        try:
            r = sess.get(f"{BASE}/{cat}", timeout=25)
            if r.status_code != 200:
                logger.warning("auctions: category %s -> HTTP %s", cat, r.status_code)
                continue
        except Exception as exc:
            logger.warning("auctions: category %s failed: %s", cat, exc)
            continue
        n = 0
        for url in DETAIL_RE.findall(r.text):
            lid = _listing_id(url)
            if lid in seen:
                continue
            seen.add(lid)
            detail_urls.append((url, cat))
            n += 1
            if n >= per_cat:
                break
        time.sleep(0.4)

    # 2. parse each detail page
    from src.db import is_notice_processed, mark_notice_processed
    out: list[AnalysedNotice] = []
    for url, cat in detail_urls[:max_listings]:
        nid = f"RO-AUC-{_listing_id(url)}"
        if is_notice_processed(nid):
            continue
        try:
            r = sess.get(url, timeout=25)
            if r.status_code != 200:
                continue
            html = r.text
            soup = BeautifulSoup(html, "html.parser")
            title = (soup.title.get_text() if soup.title else "").split("-")[0].strip()
            body = soup.get_text(" ", strip=True)

            firm_m = FIRM_RE.search(body)
            firm = re.sub(r"\s{2,}", " ", firm_m.group(1)).strip() if firm_m else ""
            # debtor: a company-suffixed name in the title that isn't the practitioner firm
            debtor = ""
            for cand in COMPANY_RE.findall(title) or COMPANY_RE.findall(body[:400]):
                if firm and cand.upper().split()[0] in firm.upper():
                    continue
                debtor = cand.strip()
                break
            # practitioner email: prefer one not on the portal domain
            emails = [e for e in EMAIL_RE.findall(html)
                      if "licitatii-insolventa" not in e and "sentry" not in e.lower()]
            email = emails[0] if emails else ""

            # If the notice text didn't yield a firm, derive a display name from
            # the email domain (skip free-mail providers where that's meaningless).
            FREEMAIL = {"yahoo", "gmail", "hotmail", "outlook", "yahoo.com", "icloud"}
            if not firm and email:
                dom = email.split("@")[1].split(".")[0].lower()
                if dom not in FREEMAIL:
                    firm = dom.replace("-", " ").replace("_", " ").title()
            price_m = PRICE_RE.search(body)
            price = f"{price_m.group(1).strip()} {price_m.group(2).upper()}" if price_m else ""

            cat_name, cat_bonus = CATEGORIES[cat]
            score = 45 + cat_bonus + (10 if email else 0)
            score = min(score, 100)
            category = "HIGH" if score >= 70 else ("MEDIUM" if score >= 45 else "LOW")

            n = AnalysedNotice()
            n.country = "RO"
            n.notice_id = nid
            n.notice_url = url
            n.ch_url = url
            n.notice_type = "Asset sale / auction (Romania)"
            n.company_name = "🇷🇴 " + (debtor or title or "Romanian asset sale")
            n.sector = cat_name
            n.registered_address = ""
            n.estimated_assets = [title] if title else []
            signals = [f"Asset sale live on UNPIR auction portal ({cat_name})"]
            if price:
                signals.append(f"Listed price: {price}")
            n.opportunity_signals = signals
            n.opportunity_score = score
            n.opportunity_category = category
            if firm or email:
                p = AucPractitioner(name=firm or "Selling practitioner",
                                    role="Lichidator / administrator judiciar",
                                    firm=firm, email=email)
                n.practitioners = [p]
                if email:
                    n.ip_email = email
                    n.draft_email_subject = f"Expression of interest - {debtor or 'asset sale'}"
            out.append(n)
        except Exception:
            logger.exception("auctions: error parsing %s", url)
        finally:
            mark_notice_processed(nid, url, "")
        time.sleep(0.4)

    logger.info("Romania auctions: %d asset-sale opportunities", len(out))
    return out
