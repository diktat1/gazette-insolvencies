"""Free Indian insolvency feed via the IBBI public-announcement register.

The Insolvency and Bankruptcy Board of India publishes every CIRP, liquidation,
voluntary-liquidation and auction public announcement (Form A etc.) in a single
server-rendered, English-language table at ibbi.gov.in/public-announcement,
newest-first and paginated. Each row already carries the corporate debtor, the
applicant, the insolvency professional, the debtor's address, the dates and a
link to the announcement PDF - everything we need to seed the pipeline.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from src import config

logger = logging.getLogger(__name__)

LIST_URL = "https://ibbi.gov.in/public-announcement"
WEB_BASE = "https://ibbi.gov.in"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@dataclass
class InEntry:
    pa_type: str
    announce_date: str       # ISO date or ""
    submission_deadline: str
    debtor: str
    applicant: str
    ip_name: str             # insolvency professional
    address: str
    pdf_url: str

    @property
    def notice_id(self) -> str:
        # The announcement PDF basename is unique per filing; fall back to a
        # debtor+date key when no PDF is linked.
        if self.pdf_url:
            return "IN-" + self.pdf_url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return f"IN-{self.debtor[:40]}-{self.announce_date}".replace(" ", "_")


def _parse_in_date(s: str) -> str:
    s = (s or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _extract_pdf(cells) -> str:
    """Find the announcement-PDF link anywhere in the row.

    The IBBI table layout varies (the optional address column shifts the PDF
    cell), so scan every cell's anchor rather than trust a fixed index.
    """
    import re
    for cell in cells:
        for a in cell.find_all("a"):
            blob = (a.get("onclick") or "") + " " + (a.get("href") or "")
            m = re.search(r"https?://[^'\"\s)]+\.pdf", blob, re.IGNORECASE)
            if m:
                return m.group(0).replace("ibbi.gov.in//", "ibbi.gov.in/")
            href = a.get("href") or ""
            if href.lower().endswith(".pdf"):
                return (WEB_BASE + href) if href.startswith("/") else href
    return ""


def _looks_like_address(text: str) -> bool:
    import re
    return len(text) > 25 and "," in text and bool(re.search(r"\d", text))


def fetch_in_entries(lookback_days: int = 1, max_companies: int = 200) -> list[InEntry]:
    """Collect recent IBBI public announcements, newest first.

    Pages the register until the announcement date falls outside `lookback_days`
    or `max_companies` is reached.
    """
    cutoff = date.today() - timedelta(days=max(1, lookback_days))
    out: list[InEntry] = []
    seen: set[str] = set()

    for page in range(config.INDIA_MAX_PAGES):
        params = {"page": page, "sort": "FLD_PA_ANNOUNCE_DATE", "direction": "desc"}
        try:
            resp = requests.get(LIST_URL, params=params, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("IBBI page %d failed: %s", page, exc)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")
        data_rows = 0
        stop = False
        for tr in rows:
            cells = tr.find_all("td")
            if len(cells) < 6:
                continue
            data_rows += 1
            announce = _parse_in_date(cells[1].get_text(strip=True))
            if announce and announce < cutoff.isoformat():
                stop = True
                break

            # First six columns are fixed; the address column is optional and the
            # PDF link can sit in any trailing cell, so resolve those flexibly.
            address = ""
            for c in cells[6:]:
                txt = c.get_text(" ", strip=True)
                if _looks_like_address(txt):
                    address = txt
                    break

            entry = InEntry(
                pa_type=cells[0].get_text(" ", strip=True),
                announce_date=announce,
                submission_deadline=_parse_in_date(cells[2].get_text(strip=True)),
                debtor=cells[3].get_text(" ", strip=True),
                applicant=cells[4].get_text(" ", strip=True),
                ip_name=cells[5].get_text(" ", strip=True),
                address=address,
                pdf_url=_extract_pdf(cells),
            )
            if not entry.debtor or entry.notice_id in seen:
                continue
            seen.add(entry.notice_id)
            out.append(entry)
            if len(out) >= max_companies:
                return out

        if stop or data_rows == 0:
            break
        time.sleep(config.INDIA_REQUEST_PAUSE)

    return out
