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


def lookup_practitioner(dosar: str) -> dict:
    """Return {firm, role} for the case's practitioner, or {} if not found."""
    if not dosar:
        return {}
    # ECRIS matches the base case number; strip annex suffixes like '/a14'.
    base = re.sub(r"/a\d+$", "", dosar.strip(), flags=re.I)
    try:
        r = requests.post(ENDPOINT, data=_ENVELOPE.format(dosar=base).encode("utf-8"),
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        logger.debug("ECRIS lookup failed for %s: %s", dosar, exc)
        return {}

    parties = _PARTY_RE.findall(r.text)
    # Prefer a clean firm-looking name (ends in SPRL/IPURL) with a practitioner role.
    candidates = [(n, c) for n, c in parties if _PRACTITIONER_ROLE.search(c)]
    if not candidates:
        return {}
    candidates.sort(key=lambda nc: (0 if re.search(r"\b(SPRL|IPURL)\b", nc[0], re.I) else 1,
                                    len(nc[0])))
    nume, calitate = candidates[0]
    time.sleep(0.4)
    return {"firm": _clean_firm(nume), "role": _unescape(calitate)}
