"""Malaysian corporate winding-up feed via the MdI e-Insolvensi portal.

Malaysia has no free, public, machine-readable register of corporate winding-up
notices: the Department of Insolvency (MdI) e-Insolvensi portal is the
authoritative source but sits behind a login, the Federal Gazette is PDF-only,
and data.gov.my carries no insolvency dataset. This module therefore drives the
e-Insolvensi portal with credentials supplied via the environment
(MALAYSIA_USER / MALAYSIA_PASS) and is wired OFF by default.

The portal is ASP.NET WebForms: log in by replaying the page's hidden
__VIEWSTATE / __EVENTVALIDATION fields with the login button, then read the
winding-up search results. WebForms field names are stable but the post-login
results-table layout still needs confirming against a live logged-in session;
until then the feed fails closed (returns []), so it can never break the report.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from src import config

logger = logging.getLogger(__name__)

PORTAL = "https://e-insolvensi.mdi.gov.my/"
SEARCH_PAGE = "https://e-insolvensi.mdi.gov.my/CarianSyarikat.aspx"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

USER_FIELD = "ctl00$ContentPlaceHolder1$usernametxt"
PASS_FIELD = "ctl00$ContentPlaceHolder1$passwordtxt"
LOGIN_BUTTON = "ctl00$ContentPlaceHolder1$btnLogMasuk"


@dataclass
class MyEntry:
    company_name: str
    company_number: str
    notice_type: str
    notice_date: str         # ISO date or ""
    court: str
    detail_url: str

    @property
    def notice_id(self) -> str:
        return f"MY-{self.company_number or self.company_name[:40].replace(' ', '_')}"


def _hidden_fields(soup: BeautifulSoup) -> dict:
    out = {}
    for f in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
              "__EVENTTARGET", "__EVENTARGUMENT"):
        el = soup.find("input", {"name": f})
        out[f] = el.get("value", "") if el else ""
    return out


def _login(session: requests.Session) -> bool:
    """Replay the WebForms login form with credentials. Returns True on success."""
    resp = session.get(PORTAL, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    form = _hidden_fields(soup)
    form.update({
        USER_FIELD: config.MALAYSIA_USER,
        PASS_FIELD: config.MALAYSIA_PASS,
        LOGIN_BUTTON: "Log Masuk",
    })
    post = session.post(PORTAL, data=form, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
    post.raise_for_status()
    # A successful login leaves the login textboxes behind; their absence is the
    # signal that we are now inside the authenticated portal.
    return USER_FIELD not in post.text


def _parse_results(html: str, cutoff: date) -> list[MyEntry]:
    """Parse the winding-up results table.

    NOTE: column order is provisional and must be confirmed against a live
    logged-in session; parsing fails closed (skips malformed rows).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[MyEntry] = []
    for tr in soup.select("table tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) < 4:
            continue
        notice_date = ""
        for c in cells:
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    notice_date = datetime.strptime(c, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            if notice_date:
                break
        if notice_date and notice_date < cutoff.isoformat():
            continue
        out.append(MyEntry(
            company_name=cells[0], company_number=cells[1] if len(cells) > 1 else "",
            notice_type=cells[2] if len(cells) > 2 else "Winding-up",
            notice_date=notice_date, court="", detail_url=SEARCH_PAGE,
        ))
    return out


def fetch_my_entries(lookback_days: int = 1, max_companies: int = 100) -> list[MyEntry]:
    """Collect recent winding-up notices from e-Insolvensi. Fails closed."""
    if not (config.MALAYSIA_USER and config.MALAYSIA_PASS):
        logger.warning("Malaysia: MALAYSIA_USER/MALAYSIA_PASS not set - skipping (login required)")
        return []

    cutoff = date.today() - timedelta(days=max(1, lookback_days))
    try:
        with requests.Session() as s:
            if not _login(s):
                logger.warning("Malaysia: e-Insolvensi login failed - skipping")
                return []
            resp = s.get(SEARCH_PAGE, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            return _parse_results(resp.text, cutoff)[:max_companies]
    except Exception as exc:
        logger.warning("Malaysia: feed failed (%s) - skipping", exc)
        return []
