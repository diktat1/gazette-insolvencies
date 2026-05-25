"""Free US corporate-bankruptcy feed via CourtListener (free.law).

CourtListener's v4 search API exposes the RECAP archive of federal court
dockets, including every US bankruptcy court, with no key required (an optional
token raises the rate limit). We query the configured bankruptcy courts newest
-first and keep corporate Chapter 7 (liquidation) and Chapter 11 (reorganisation
/ 363 asset sale) filings - the chapters where there are assets or a business to
buy. Chapter 13 (individual) and the rest are dropped.

Each hit already carries case name, chapter, court, filing date, docket number
and a docket link, which is enough to seed the pipeline.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests

from src import config

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
WEB_BASE = "https://www.courtlistener.com"
KEEP_CHAPTERS = {"7", "11"}


@dataclass
class UsEntry:
    case_name: str
    chapter: str
    court_id: str
    court_name: str
    docket_number: str
    date_filed: str          # ISO date or ""
    docket_url: str
    pacer_case_id: str = ""
    parties: list = field(default_factory=list)

    @property
    def notice_id(self) -> str:
        return f"US-{self.court_id}-{self.docket_number}".strip()


def _headers() -> dict:
    h = {"User-Agent": "insolvency-pipeline/1.0", "Accept": "application/json"}
    if config.COURTLISTENER_TOKEN:
        h["Authorization"] = f"Token {config.COURTLISTENER_TOKEN}"
    return h


def fetch_us_entries(lookback_days: int = 1, max_companies: int = 200) -> list[UsEntry]:
    """Collect recent corporate bankruptcy filings, newest first.

    Pages CourtListener (ordered by dateFiled desc) across the configured
    bankruptcy courts until the filing date falls outside `lookback_days` or
    `max_companies` is reached.
    """
    cutoff = date.today() - timedelta(days=max(1, lookback_days))
    courts = " ".join(config.USA_COURTS) if config.USA_COURTS else None

    out: list[UsEntry] = []
    seen: set[str] = set()
    url = SEARCH_URL
    params = {"type": "r", "order_by": "dateFiled desc"}
    if courts:
        params["court"] = courts

    for _ in range(config.USA_MAX_PAGES):
        try:
            resp = requests.get(url, params=params if url == SEARCH_URL else None,
                                headers=_headers(), timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("CourtListener fetch failed: %s", exc)
            break

        results = data.get("results", []) or []
        if not results:
            break

        stop = False
        for r in results:
            df = r.get("dateFiled") or ""
            if df:
                try:
                    if datetime.fromisoformat(df).date() < cutoff:
                        stop = True
                        break
                except ValueError:
                    pass

            chapter = str(r.get("chapter") or "").strip()
            if chapter not in KEEP_CHAPTERS:
                continue

            docket_no = str(r.get("docketNumber") or "").strip()
            court_id = r.get("court_id") or ""
            key = f"{court_id}-{docket_no}"
            if not docket_no or key in seen:
                continue
            seen.add(key)

            abs_url = r.get("docket_absolute_url") or ""
            out.append(UsEntry(
                case_name=(r.get("caseName") or "").strip(),
                chapter=chapter,
                court_id=court_id,
                court_name=r.get("court") or "",
                docket_number=docket_no,
                date_filed=df,
                docket_url=(WEB_BASE + abs_url) if abs_url else "",
                pacer_case_id=str(r.get("pacer_case_id") or ""),
                parties=[p for p in (r.get("party") or []) if p],
            ))
            if len(out) >= max_companies:
                return out

        nxt = data.get("next")
        if stop or not nxt:
            break
        url = nxt
        time.sleep(config.USA_REQUEST_PAUSE)

    return out
