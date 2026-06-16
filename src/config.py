import os
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Gazette feed configuration
# ---------------------------------------------------------------------------
# Category 24 = Corporate Insolvency (all types: winding-up, administration,
# receivership, CVL, meetings of creditors, voluntary arrangements)
GAZETTE_CATEGORY_CODES = ["24"]

# Base URL for the Gazette feed
# Using all-notices endpoint with category filter (more reliable than /insolvency/)
GAZETTE_FEED_BASE = "https://www.thegazette.co.uk/all-notices/notice"

# Individual notice detail (HTML)
GAZETTE_NOTICE_URL = "https://www.thegazette.co.uk/notice/"

# Page size for feed pagination
GAZETTE_PAGE_SIZE = 100

# How many days back to look for new notices
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "1"))

# ---------------------------------------------------------------------------
# Companies House API
# ---------------------------------------------------------------------------
COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "")
COMPANIES_HOUSE_BASE_URL = "https://api.company-information.service.gov.uk"

# ---------------------------------------------------------------------------
# Email / SMTP
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_CC = [e.strip() for e in os.getenv("EMAIL_CC", "").split(",") if e.strip()]

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
DAILY_SEND_TIME = os.getenv("DAILY_SEND_TIME", "08:00")

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
MIN_OPPORTUNITY_SCORE = int(os.getenv("MIN_OPPORTUNITY_SCORE", "0"))

# ---------------------------------------------------------------------------
# Romania module (BPI via lege5.ro index + ANAF enrichment)
# ---------------------------------------------------------------------------
ROMANIA_ENABLED = os.getenv("ROMANIA_ENABLED", "false").lower() in ("true", "1", "yes")
ROMANIA_LOOKBACK_DAYS = int(os.getenv("ROMANIA_LOOKBACK_DAYS", "1"))
# Scan ceiling for the daily feed - set high so we capture the whole day's
# filings (the lookback-date cutoff is what actually bounds it to ~1 day).
ROMANIA_MAX_COMPANIES = int(os.getenv("ROMANIA_MAX_COMPANIES", "2000"))
# Deep-enrichment guards. ANAF financials are rate-limited to ~1 req/s, so on a
# high-volume day (1000+ asset-rich candidates) a naive serial enrich runs for
# hours and the GitHub job hits its 6h cap. Two hard bounds keep the run finite:
#   - MAX_ENRICH caps how many candidates get the deep ANAF + ECRIS enrichment.
#   - TIME_BUDGET_S stops enrichment after N seconds and emits what we have.
# Whichever bites first wins; any candidate not enriched is logged and marked
# processed so it does not pile up into an ever-growing backlog.
ROMANIA_MAX_ENRICH = int(os.getenv("ROMANIA_MAX_ENRICH", "400"))
ROMANIA_TIME_BUDGET_S = int(os.getenv("ROMANIA_TIME_BUDGET_S", "2400"))
# Enrichment runs in a thread pool: the per-candidate work is latency-bound
# (ANAF financials + a slow ECRIS lookup), so concurrency lets those overlap.
# ANAF financials stay globally throttled to ~1 req/s inside the client.
ROMANIA_CONCURRENCY = int(os.getenv("ROMANIA_CONCURRENCY", "6"))
# Auction feed (licitatii-insolventa.ro): live asset sales with practitioner
# contact on the page. On by default when Romania is enabled.
ROMANIA_AUCTIONS_ENABLED = os.getenv("ROMANIA_AUCTIONS_ENABLED", "true").lower() in ("true", "1", "yes")
ROMANIA_AUCTIONS_MAX = int(os.getenv("ROMANIA_AUCTIONS_MAX", "30"))

# ---------------------------------------------------------------------------
# USA module (CourtListener free RECAP search - Chapter 7/11)
# ---------------------------------------------------------------------------
USA_ENABLED = os.getenv("USA_ENABLED", "false").lower() in ("true", "1", "yes")
USA_LOOKBACK_DAYS = int(os.getenv("USA_LOOKBACK_DAYS", "1"))
USA_MAX_COMPANIES = int(os.getenv("USA_MAX_COMPANIES", "200"))
USA_MAX_PAGES = int(os.getenv("USA_MAX_PAGES", "20"))
USA_REQUEST_PAUSE = float(os.getenv("USA_REQUEST_PAUSE", "1.0"))
# Optional CourtListener token raises the anonymous rate limit.
COURTLISTENER_TOKEN = os.getenv("COURTLISTENER_TOKEN", "")
# High-volume corporate-bankruptcy courts to scan (space-/comma-separated court
# ids). Empty = scan all federal dockets and filter to Chapter 7/11 client-side.
USA_COURTS = [c.strip() for c in os.getenv(
    "USA_COURTS", "deb nysb txsb txnb njb cacb ilnb flsb").replace(",", " ").split() if c.strip()]

# ---------------------------------------------------------------------------
# Turkey module (ilan.gov.tr / Basın İlan Kurumu - bankruptcy-law notices)
# ---------------------------------------------------------------------------
TURKEY_ENABLED = os.getenv("TURKEY_ENABLED", "false").lower() in ("true", "1", "yes")
TURKEY_LOOKBACK_DAYS = int(os.getenv("TURKEY_LOOKBACK_DAYS", "1"))
TURKEY_MAX_COMPANIES = int(os.getenv("TURKEY_MAX_COMPANIES", "60"))
TURKEY_MAX_PAGES = int(os.getenv("TURKEY_MAX_PAGES", "40"))
TURKEY_REQUEST_PAUSE = float(os.getenv("TURKEY_REQUEST_PAUSE", "0.5"))

# ---------------------------------------------------------------------------
# Malaysia module (MdI e-Insolvensi - credential-gated, OFF by default)
# ---------------------------------------------------------------------------
MALAYSIA_ENABLED = os.getenv("MALAYSIA_ENABLED", "false").lower() in ("true", "1", "yes")
MALAYSIA_LOOKBACK_DAYS = int(os.getenv("MALAYSIA_LOOKBACK_DAYS", "1"))
MALAYSIA_MAX_COMPANIES = int(os.getenv("MALAYSIA_MAX_COMPANIES", "100"))
MALAYSIA_USER = os.getenv("MALAYSIA_USER", "")
MALAYSIA_PASS = os.getenv("MALAYSIA_PASS", "")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "gazette_tracker.db")

# ---------------------------------------------------------------------------
# Request settings
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = 30  # seconds for general API requests

# DuckDuckGo often blocks/times out - use a shorter timeout
DUCKDUCKGO_TIMEOUT = int(os.getenv("DUCKDUCKGO_TIMEOUT", "5"))  # seconds
REQUEST_HEADERS = {
    "User-Agent": "GazetteInsolvencyAnalyser/1.0",
    "Accept": "application/atom+xml",
}
