"""
Companies House API integration.

Looks up companies by number or name to get:
- Company profile (status, SIC codes, type, address)
- Filing history (accounts, confirmation statements, insolvency filings)
- Charges (secured debt – tangible assets)
- Insolvency case details (IPs, dates, case type)
- Officers (directors, secretaries)

Free API: https://developer.company-information.service.gov.uk/
Rate limit: 600 requests per 5 minutes.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional

import requests

from src import config

logger = logging.getLogger(__name__)

# Thread-safe Companies House rate limiter. CH allows ~600 req / 5 min (~2/s);
# a global min-interval throttle keeps concurrent workers under the limit so
# parallelising the per-notice loop doesn't trigger 429 storms.
_CH_RATE_LOCK = threading.Lock()
_CH_LAST_CALL = [0.0]
_CH_MIN_INTERVAL = float(os.getenv("CH_MIN_INTERVAL", "0.5"))
_CACHE_LOCK = threading.Lock()


def _throttle_ch() -> None:
    with _CH_RATE_LOCK:
        wait = _CH_MIN_INTERVAL - (time.monotonic() - _CH_LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _CH_LAST_CALL[0] = time.monotonic()

# ---------------------------------------------------------------------------
# Cache for Companies House lookups
# ---------------------------------------------------------------------------
_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ch_cache.json")
_CACHE_TTL_HOURS = 24  # How long to keep cached data
_cache: dict = {}
_cache_loaded = False


def _load_cache() -> None:
    """Load cache from disk."""
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, 'r') as f:
                _cache = json.load(f)
            # Clean expired entries
            now = datetime.utcnow().isoformat()
            expired = [k for k, v in _cache.items() if v.get('expires', '') < now]
            for k in expired:
                del _cache[k]
            logger.debug("Loaded CH cache with %d entries (%d expired)", len(_cache) + len(expired), len(expired))
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Could not load CH cache: %s", e)
        _cache = {}
    _cache_loaded = True


def _save_cache() -> None:
    """Save cache to disk (lock-guarded for thread safety)."""
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with _CACHE_LOCK:
            with open(_CACHE_FILE, 'w') as f:
                json.dump(_cache, f)
    except IOError as e:
        logger.warning("Could not save CH cache: %s", e)


def _get_cached(key: str) -> Optional[dict]:
    """Get a cached API response if not expired."""
    _load_cache()
    entry = _cache.get(key)
    if not entry:
        return None
    if entry.get('expires', '') < datetime.utcnow().isoformat():
        del _cache[key]
        return None
    logger.debug("CH cache hit for %s", key)
    return entry.get('data')


def _set_cached(key: str, data: dict) -> None:
    """Cache an API response."""
    _load_cache()
    expires = (datetime.utcnow() + timedelta(hours=_CACHE_TTL_HOURS)).isoformat()
    _cache[key] = {'data': data, 'expires': expires}
    _save_cache()

CH_WEB_BASE = "https://find-and-update.company-information.service.gov.uk"


@dataclass
class FilingRecord:
    date: str = ""
    category: str = ""         # e.g. "accounts", "confirmation-statement", "insolvency"
    description: str = ""
    filing_type: str = ""      # e.g. "AA", "CS01", "AM01"
    document_url: str = ""     # link to view the document on CH


@dataclass
class InsolvencyCase:
    case_number: int = 0
    case_type: str = ""        # e.g. "compulsory-liquidation", "administration"
    practitioner_names: list = field(default_factory=list)
    practitioner_addresses: list = field(default_factory=list)
    dates: dict = field(default_factory=dict)  # e.g. {"wound-up-on": "2025-01-15"}


@dataclass
class CompanyProfile:
    company_number: str = ""
    company_name: str = ""
    company_status: str = ""
    company_type: str = ""
    date_of_creation: str = ""
    date_of_cessation: str = ""
    sic_codes: list = field(default_factory=list)
    registered_address: str = ""
    has_charges: bool = False
    has_insolvency_history: bool = False
    last_accounts_date: str = ""
    last_accounts_type: str = ""
    next_accounts_due: str = ""
    confirmation_statement_overdue: bool = False
    companies_house_url: str = ""
    officers: list = field(default_factory=list)

    # Substance indicators
    has_filed_full_accounts: bool = False
    has_recent_activity: bool = False
    accounts_overdue: bool = False

    # Filing history
    filing_history_url: str = ""
    recent_filings: list = field(default_factory=list)   # List[FilingRecord]
    total_filings: int = 0
    last_filing_date: str = ""
    has_accounts_filings: bool = False
    accounts_category_count: int = 0

    # Insolvency details
    insolvency_cases: list = field(default_factory=list)  # List[InsolvencyCase]

    # Charges detail
    total_charges: int = 0
    outstanding_charges: int = 0
    satisfied_charges: int = 0

    # Phantom company signals
    is_likely_phantom: bool = False
    phantom_reasons: list = field(default_factory=list)


def _api_get(endpoint: str, params: Optional[dict] = None, use_cache: bool = True) -> Optional[dict]:
    """Make an authenticated GET request to the Companies House API."""
    if not config.COMPANIES_HOUSE_API_KEY:
        logger.warning("No Companies House API key configured – skipping lookup")
        return None

    # Build cache key from endpoint and params
    cache_key = endpoint
    if params:
        cache_key += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    # Check cache first
    if use_cache:
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached

    url = f"{config.COMPANIES_HOUSE_BASE_URL}{endpoint}"
    _throttle_ch()
    try:
        resp = requests.get(
            url,
            params=params,
            auth=(config.COMPANIES_HOUSE_API_KEY, ""),
            timeout=config.REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            logger.debug("Companies House 404 for %s", endpoint)
            return None
        if resp.status_code == 429:
            logger.warning("Companies House rate limit hit – backing off 10s")
            time.sleep(10)
            return _api_get(endpoint, params, use_cache=False)
        resp.raise_for_status()
        data = resp.json()

        # Cache successful responses
        if use_cache and data:
            _set_cached(cache_key, data)

        return data
    except requests.RequestException as exc:
        logger.error("Companies House API error for %s: %s", endpoint, exc)
        return None


# ---------------------------------------------------------------------------
# Core lookup
# ---------------------------------------------------------------------------

def lookup_by_number(company_number: str) -> Optional[CompanyProfile]:
    """Look up a company by its Companies House registration number."""
    if not company_number:
        return None

    num = company_number.strip().upper()
    if num.isdigit():
        num = num.zfill(8)

    data = _api_get(f"/company/{num}")
    if not data:
        return None

    profile = _build_profile(data)

    # Enrich with filing history, charges, and insolvency data
    _enrich_filing_history(profile)
    _enrich_charges(profile)
    if profile.has_insolvency_history:
        _enrich_insolvency(profile)

    # Run phantom detection after enrichment
    _detect_phantom(profile)

    return profile


def search_by_name(company_name: str) -> Optional[CompanyProfile]:
    """
    Search Companies House by company name and return the best match.
    Fallback when we don't have a company number from the notice.
    """
    if not company_name:
        return None

    data = _api_get("/search/companies", params={"q": company_name, "items_per_page": 5})
    if not data or not data.get("items"):
        return None

    # Try to find an exact-ish match
    name_upper = company_name.upper().strip()
    for item in data["items"]:
        if item.get("title", "").upper().strip() == name_upper:
            return lookup_by_number(item.get("company_number", ""))

    # Fall back to first result if close enough
    first = data["items"][0]
    first_name = first.get("title", "").upper()
    name_words = set(name_upper.replace("LIMITED", "LTD").split())
    first_words = set(first_name.replace("LIMITED", "LTD").split())
    overlap = name_words & first_words
    if len(overlap) >= len(name_words) * 0.6:
        return lookup_by_number(first.get("company_number", ""))

    logger.info("No close Companies House match for '%s'", company_name)
    return None


# ---------------------------------------------------------------------------
# Profile builder
# ---------------------------------------------------------------------------

def _build_profile(data: dict) -> CompanyProfile:
    """Build a CompanyProfile from the Companies House company endpoint response."""
    profile = CompanyProfile()
    profile.company_number = data.get("company_number", "")
    profile.company_name = data.get("company_name", "")
    profile.company_status = data.get("company_status", "")
    profile.company_type = data.get("type", "")
    profile.date_of_creation = data.get("date_of_creation", "")
    profile.date_of_cessation = data.get("date_of_cessation", "")
    profile.sic_codes = data.get("sic_codes", [])
    profile.has_charges = data.get("has_charges", False)
    profile.has_insolvency_history = data.get("has_insolvency_history", False)
    profile.companies_house_url = f"{CH_WEB_BASE}/company/{profile.company_number}"
    profile.filing_history_url = f"{CH_WEB_BASE}/company/{profile.company_number}/filing-history"

    # Registered address
    addr = data.get("registered_office_address", {})
    parts = [
        addr.get("premises", ""),
        addr.get("address_line_1", ""),
        addr.get("address_line_2", ""),
        addr.get("locality", ""),
        addr.get("region", ""),
        addr.get("postal_code", ""),
        addr.get("country", ""),
    ]
    profile.registered_address = ", ".join(p for p in parts if p)

    # Accounts
    accounts = data.get("accounts", {})
    last_acc = accounts.get("last_accounts", {})
    profile.last_accounts_date = last_acc.get("made_up_to", "")
    profile.last_accounts_type = last_acc.get("type", "")
    profile.has_filed_full_accounts = profile.last_accounts_type in (
        "full", "group", "medium", "small", "audit-exemption-subsidiary",
    )

    next_acc = accounts.get("next_accounts", {})
    profile.next_accounts_due = next_acc.get("due_on", "")
    profile.accounts_overdue = accounts.get("overdue", False)

    # Confirmation statement
    conf = data.get("confirmation_statement", {})
    profile.confirmation_statement_overdue = conf.get("overdue", False)

    # Recent activity: check if last accounts were filed in last 2 years
    if profile.last_accounts_date:
        try:
            last_acc_date = datetime.strptime(profile.last_accounts_date, "%Y-%m-%d")
            profile.has_recent_activity = (datetime.utcnow() - last_acc_date).days < 730
        except (ValueError, TypeError):
            pass

    return profile


# ---------------------------------------------------------------------------
# Filing history enrichment
# ---------------------------------------------------------------------------

def _enrich_filing_history(profile: CompanyProfile) -> None:
    """Fetch filing history and extract key details."""
    num = profile.company_number
    if not num:
        return

    data = _api_get(
        f"/company/{num}/filing-history",
        params={"items_per_page": 25, "category": "accounts,insolvency"},
    )
    if not data:
        return

    profile.total_filings = data.get("total_count", 0)

    for item in data.get("items", []):
        filing = FilingRecord()
        filing.date = item.get("date", "")
        filing.category = item.get("category", "")
        filing.description = _filing_description(item)
        filing.filing_type = item.get("type", "")

        # Build document URL
        links = item.get("links", {})
        doc_meta = links.get("document_metadata", "")
        if doc_meta:
            filing.document_url = f"{CH_WEB_BASE}{links.get('self', '')}"
        elif links.get("self"):
            filing.document_url = f"{CH_WEB_BASE}{links['self']}"

        profile.recent_filings.append(filing)

        if filing.category == "accounts":
            profile.accounts_category_count += 1
            profile.has_accounts_filings = True

    # Track last filing date
    if profile.recent_filings:
        profile.last_filing_date = profile.recent_filings[0].date


def _filing_description(item: dict) -> str:
    """Build a human-readable description from a filing item."""
    desc = item.get("description", "")
    desc_values = item.get("description_values", {})

    # Replace template placeholders like {made_up_date}
    if desc_values:
        for key, val in desc_values.items():
            desc = desc.replace(f"{{{key}}}", str(val))

    return desc


# ---------------------------------------------------------------------------
# Charges enrichment
# ---------------------------------------------------------------------------

def _enrich_charges(profile: CompanyProfile) -> None:
    """Fetch charges (secured debt) details."""
    if not profile.has_charges:
        return

    num = profile.company_number
    data = _api_get(f"/company/{num}/charges", params={"items_per_page": 25})
    if not data:
        return

    profile.total_charges = data.get("total_count", 0)

    for item in data.get("items", []):
        status = item.get("status", "")
        if status in ("outstanding", "part-satisfied"):
            profile.outstanding_charges += 1
        elif status in ("fully-satisfied", "satisfied"):
            profile.satisfied_charges += 1


# ---------------------------------------------------------------------------
# Insolvency enrichment
# ---------------------------------------------------------------------------

def _enrich_insolvency(profile: CompanyProfile) -> None:
    """Fetch insolvency case details from Companies House."""
    num = profile.company_number
    data = _api_get(f"/company/{num}/insolvency")
    if not data:
        return

    for case_data in data.get("cases", []):
        case = InsolvencyCase()
        case.case_number = case_data.get("number", 0)
        case.case_type = case_data.get("type", "")

        # Practitioners
        for prac in case_data.get("practitioners", []):
            name = prac.get("name", "")
            if name:
                case.practitioner_names.append(name)
            addr = prac.get("address", {})
            if addr:
                addr_parts = [
                    addr.get("address_line_1", ""),
                    addr.get("address_line_2", ""),
                    addr.get("locality", ""),
                    addr.get("postal_code", ""),
                ]
                case.practitioner_addresses.append(", ".join(p for p in addr_parts if p))

        # Dates
        for date_entry in case_data.get("dates", []):
            date_type = date_entry.get("type", "")
            date_val = date_entry.get("date", "")
            if date_type and date_val:
                case.dates[date_type] = date_val

        profile.insolvency_cases.append(case)


# ---------------------------------------------------------------------------
# Phantom company detection
# ---------------------------------------------------------------------------

def _detect_phantom(profile: CompanyProfile) -> None:
    """
    Determine if the company is likely a phantom / shell / non-trading entity.

    Signals that a company is phantom:
    - No accounts ever filed
    - Only micro-entity or dormant accounts
    - Confirmation statement overdue (not maintaining the company)
    - Accounts overdue
    - No filing history at all
    - Registered at a known formation agent address
    - Very recently incorporated (less than 1 year) with no filings
    """
    reasons: list[str] = []

    # No accounts ever filed
    if not profile.last_accounts_date and not profile.has_accounts_filings:
        if profile.date_of_creation:
            try:
                created = datetime.strptime(profile.date_of_creation, "%Y-%m-%d")
                age_months = (datetime.utcnow() - created).days / 30.44
                # New companies get ~21 months before first accounts are due
                if age_months > 24:
                    reasons.append("No accounts ever filed despite being >2 years old")
            except (ValueError, TypeError):
                pass

    # Only dormant accounts
    if profile.last_accounts_type == "dormant":
        reasons.append("Only dormant accounts filed – company not trading")

    # Only micro-entity with no charges and no recent filings
    if profile.last_accounts_type == "micro-entity" and not profile.has_charges:
        reasons.append("Micro-entity accounts with no secured charges – minimal substance")

    # Confirmation statement overdue
    if profile.confirmation_statement_overdue:
        reasons.append("Confirmation statement overdue – company may be abandoned")

    # Accounts overdue
    if profile.accounts_overdue:
        reasons.append("Accounts overdue – company may not be actively managed")

    # No filings at all
    if profile.total_filings == 0:
        reasons.append("No filing history found on Companies House")

    # Very few filings for an old company
    if profile.total_filings > 0 and profile.date_of_creation:
        try:
            created = datetime.strptime(profile.date_of_creation, "%Y-%m-%d")
            age_years = (datetime.utcnow() - created).days / 365.25
            if age_years > 3 and profile.total_filings < 3:
                reasons.append(f"Only {profile.total_filings} filings in {age_years:.0f} years – minimal activity")
        except (ValueError, TypeError):
            pass

    # Already dissolved
    if profile.company_status in ("dissolved", "closed", "converted-closed"):
        reasons.append(f"Company status: {profile.company_status}")

    # Last filing very old (more than 2 years ago)
    if profile.last_filing_date:
        try:
            last_filed = datetime.strptime(profile.last_filing_date, "%Y-%m-%d")
            days_since = (datetime.utcnow() - last_filed).days
            if days_since > 730:
                reasons.append(f"Last filing was {days_since // 365} years ago")
        except (ValueError, TypeError):
            pass

    profile.phantom_reasons = reasons
    profile.is_likely_phantom = len(reasons) >= 2


# ---------------------------------------------------------------------------
# Officers
# ---------------------------------------------------------------------------

def get_officers(company_number: str) -> list[dict]:
    """Get the list of current officers (directors, secretaries) for a company."""
    if not company_number:
        return []

    num = company_number.strip().upper()
    if num.isdigit():
        num = num.zfill(8)

    data = _api_get(f"/company/{num}/officers")
    if not data:
        return []

    officers = []
    for item in data.get("items", []):
        if item.get("resigned_on"):
            continue
        officers.append({
            "name": item.get("name", ""),
            "role": item.get("officer_role", ""),
            "appointed_on": item.get("appointed_on", ""),
            "nationality": item.get("nationality", ""),
        })

    return officers
