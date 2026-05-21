"""Free Romanian insolvency-notice index via lege5.ro.

The authoritative full BPI text is paywalled (ONRC subscription). The lege5.ro
"ultimele buletine" pages are server-rendered, paginated, and free, listing each
recent bulletin entry as:

    <li><h4><a href="/Gratuit/<slug>-cui-<CUI>-dosar-nr-<dosar>">
        NAME, CUI <CUI>, Dosar nr. <dosar></a></h4>
        BPI nr. <n> din DD/MM/YYYY</li>

That gives company name + CUI + case (dosar) number + a free detail link +
publication date, newest first - enough to seed the pipeline. Everything else
is enriched from the CUI via ANAF.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

LEGE5_PAGE = "https://lege5.ro/cautaultimelebuletine/1/{page}"
LEGE5_BASE = "https://lege5.ro"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

NAME_CUI_RE = re.compile(r"^(.*?),\s*CUI\s*(\d{2,10})\s*,\s*Dosar\s*nr\.?\s*(.+)$", re.IGNORECASE)
DATE_RE = re.compile(r"din\s*(\d{2})/(\d{2})/(\d{4})")


@dataclass
class RoEntry:
    company_name: str
    cui: str
    dosar: str
    detail_url: str
    published: str  # ISO date or ""

    @property
    def notice_id(self) -> str:
        return f"RO-{self.cui}-{self.dosar.strip()}"


def fetch_ro_entries(lookback_days: int = 1, max_companies: int = 40) -> list[RoEntry]:
    """Collect recent BPI entries, newest first.

    Pages the free index until either the publication date falls outside
    `lookback_days` or `max_companies` is reached (keeps enrichment bounded).
    """
    cutoff = date.today() - timedelta(days=max(1, lookback_days))
    max_pages = min(80, max(2, (max_companies // 10) + 3))
    seen: set[str] = set()
    out: list[RoEntry] = []

    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(LEGE5_PAGE.format(page=page), headers=HEADERS, timeout=25)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("lege5 page %d fetch failed: %s", page, exc)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        anchors = [a for a in soup.find_all("a", href=True) if "/Gratuit/" in a["href"] and "CUI" in a.get_text()]
        if not anchors:
            logger.debug("No entries on page %d; stopping.", page)
            break

        stop = False
        for a in anchors:
            m = NAME_CUI_RE.match(a.get_text(" ", strip=True))
            if not m:
                continue
            name, cui, dosar = m.group(1).strip(" .,-"), m.group(2), m.group(3).strip(" .,-")
            key = f"{cui}-{dosar}"
            if key in seen or not name:
                continue

            # Publication date sits in the <li> text right after the anchor.
            li = a.find_parent("li")
            published = ""
            if li:
                dm = DATE_RE.search(li.get_text(" ", strip=True))
                if dm:
                    dd, mm, yyyy = dm.groups()
                    try:
                        pub = date(int(yyyy), int(mm), int(dd))
                        published = pub.isoformat()
                        if pub < cutoff:
                            stop = True
                            break
                    except ValueError:
                        pass

            seen.add(key)
            out.append(RoEntry(
                company_name=name, cui=cui, dosar=dosar,
                detail_url=LEGE5_BASE + a["href"], published=published,
            ))
            if len(out) >= max_companies:
                return out

        if stop:
            logger.debug("Reached lookback cutoff %s on page %d", cutoff, page)
            break
        time.sleep(0.5)

    return out
