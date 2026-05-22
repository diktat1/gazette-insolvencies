"""Free practitioner lookup via the ECRIS court web service.

portal.just.ro exposes a public SOAP service (CautareDosare) that returns a
case's parties. In an insolvency case the administrator/lichidator judiciar is
listed as a party, so we can recover the practitioner FIRM by case number -
free, no key. Contact details are then resolved from the local directory.
"""

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

ENDPOINT = "http://portalquery.just.ro/query.asmx"
SOAP_ACTION = "portalquery.just.ro/CautareDosare"
HEADERS = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{SOAP_ACTION}"',
           "User-Agent": "Mozilla/5.0"}
TIMEOUT = 30

_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body><CautareDosare xmlns="portalquery.just.ro">'
    "<numarDosar>{dosar}</numarDosar>"
    "</CautareDosare></soap:Body></soap:Envelope>"
)

_PARTY_RE = re.compile(r"<DosarParte><nume>(.*?)</nume><calitateParte>(.*?)</calitateParte></DosarParte>", re.S)
# practitioner roles, most-specific first
_PRACTITIONER_ROLE = re.compile(r"(lichidator judiciar|administrator judiciar)", re.I)


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&#39;", "'").replace("&quot;", '"').strip())


def _clean_firm(name: str) -> str:
    """Reduce a party name to the practitioner firm.

    ECRIS sometimes gives 'SCP X SPRL - LICHIDATOR JUDICIAR AL DEBITOAREI ...';
    keep the firm part before the ' - ' qualifier, drop a leading 'SCP '/'SC '.
    """
    name = _unescape(name)
    name = re.split(r"\s+-\s+", name)[0]
    name = re.sub(r"^(SCP|SC|CABINET INDIVIDUAL|C\.I\.I\.?)\s+", "", name, flags=re.I).strip()
    return name


# Hearing-decision text and the practitioner named within it. ECRIS frequently
# names the administrator/lichidator only in the soluţie text (e.g. "desemnează
# administrator judiciar provizoriu DT RESTRUCTURING SPRL"), not as a party.
_SOL_RE = re.compile(r"<solutie(?:Sumar)?>(.*?)</solutie(?:Sumar)?>", re.S)
_PRAC_TEXT_RE = re.compile(
    r"(administrator judiciar|lichidator judiciar)(?:\s+provizoriu)?\s+"
    r"([A-ZĂÂÎŞŢ][\w&.\-]*(?:\s+[A-ZĂÂÎŞŢ0-9][\w&.\-]*){0,4}?\s+(?:SPRL|IPURL))",
    re.I,
)


def lookup_practitioner(dosar: str, company_name: str | None = None) -> dict:
    """Resolve the case's insolvency practitioner.

    Reads both the parties list AND the hearing-decision (soluţie) text, since
    the administrator/lichidator is often only named in the latter. Also reports
    whether `company_name` is the Debitor, to screen out false positives where
    the company merely appears as a creditor in someone else's case.

    Returns {firm, role, source, is_debtor} or {} if nothing found.
    """
    if not dosar:
        return {}
    base = re.sub(r"/a\d+$", "", dosar.strip(), flags=re.I)
    try:
        r = requests.post(ENDPOINT, data=_ENVELOPE.format(dosar=base).encode("utf-8"),
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        logger.debug("ECRIS lookup failed for %s: %s", dosar, exc)
        return {}
    text = r.text
    time.sleep(0.4)

    # Is the target company the debtor in this case?
    is_debtor = None
    if company_name:
        cn = re.sub(r"\s+(SRL|SA|S\.R\.L|S\.A)\.?$", "", company_name.replace("🇷🇴", "").strip(), flags=re.I)
        cn = re.escape(cn.split("(")[0].strip()[:24])
        is_debtor = bool(re.search(cn + r"[^<]*</nume><calitateParte>Debitor", text, re.I))

    # 1) Practitioner as a named party (cleanest).
    parties = [(n, c) for n, c in _PARTY_RE.findall(text) if _PRACTITIONER_ROLE.search(c)]
    parties.sort(key=lambda nc: (0 if re.search(r"\b(SPRL|IPURL)\b", nc[0], re.I) else 1, len(nc[0])))
    if parties:
        nume, calitate = parties[0]
        return {"firm": _clean_firm(nume), "role": _unescape(calitate),
                "source": "party", "is_debtor": is_debtor}

    # 2) Practitioner named in the hearing-decision text.
    for sol in _SOL_RE.findall(text):
        m = _PRAC_TEXT_RE.search(_unescape(sol))
        if m:
            firm = re.sub(r"^pe\s+", "", re.sub(r"\s+", " ", m.group(2)).strip(), flags=re.I)
            return {"firm": firm, "role": m.group(1).lower(),
                    "source": "soluţie", "is_debtor": is_debtor}

    return {"is_debtor": is_debtor} if is_debtor else {}
