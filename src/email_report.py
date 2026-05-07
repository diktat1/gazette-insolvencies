"""
Generate and send the daily insolvency opportunity email report.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src import config
from src.pdf_report import generate_pdf_report

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structure for a fully-analysed notice (passed to the template)
# ---------------------------------------------------------------------------

class AnalysedNotice:
    """All the data we've gathered about a single insolvency notice."""

    def __init__(self):
        # From Gazette
        self.notice_id: str = ""
        self.notice_url: str = ""
        self.notice_type: str = ""
        self.published_date: str = ""

        # Parsed from notice
        self.company_name: str = ""
        self.company_number: str = ""
        self.trading_name: str = ""
        self.registered_address: str = ""
        self.court_name: str = ""
        self.court_case_number: str = ""

        # Insolvency practitioners
        self.practitioners: list = []

        # Companies House – basic
        self.ch_status: str = ""
        self.ch_type: str = ""
        self.ch_sic_codes: list = []
        self.ch_url: str = ""
        self.ch_has_charges: bool = False
        self.ch_accounts_type: str = ""
        self.ch_created: str = ""

        # Companies House – filing history
        self.ch_filing_history_url: str = ""
        self.ch_total_filings: int = 0
        self.ch_recent_filings: list = []   # List[FilingRecord]

        # Companies House – insolvency
        self.ch_insolvency_cases: list = []  # List[InsolvencyCase]

        # Companies House – charges
        self.ch_total_charges: int = 0
        self.ch_outstanding_charges: int = 0

        # Companies House – phantom detection
        self.ch_is_phantom: bool = False
        self.ch_phantom_reasons: list = []

        # Website
        self.website_url: Optional[str] = None
        self.google_search_url: str = ""

        # Opportunity assessment
        self.opportunity_score: int = 0
        self.opportunity_category: str = ""
        self.opportunity_signals: list = []

        # Sector and assets
        self.sector: str = ""
        self.sector_code: str = ""
        self.estimated_assets: list = []

        # Financial info (from accounts where available)
        self.turnover: str = ""
        self.total_assets: str = ""
        self.net_assets: str = ""
        self.total_liabilities: str = ""
        self.employees: str = ""

        # Draft email for IP
        self.ip_email: str = ""
        self.draft_email_subject: str = ""
        self.draft_email_body: str = ""

        # LLM triage layer (set by src.triage_llm.apply_llm_triage)
        self.llm_tier: str = ""              # L1|L2|L3|watch|drop|unknown
        self.llm_category: str = ""          # pre_pack_buyer_search | ...
        self.llm_why: str = ""               # one-sentence rationale
        self.llm_evidence: str = ""          # short quote
        self.llm_situation: str = ""         # 2-3 sentence writeup (top N only)
        self.llm_buyer_hypothesis: str = ""  # one-sentence angle (top N only)
        self.triage_final: float = 0.0       # blended score used for ranking


def generate_email_html(notices: list[AnalysedNotice], date_str: str = "") -> str:
    """Render the email HTML from the Jinja2 template."""
    if not date_str:
        date_str = datetime.utcnow().strftime("%d %B %Y")

    # Sort by blended LLM-triage score if present, else by heuristic score
    def _sort_key(n):
        return (
            getattr(n, "triage_final", 0) or n.opportunity_score,
            n.opportunity_score,
        )
    notices_sorted = sorted(notices, key=_sort_key, reverse=True)

    # Group by category
    high = [n for n in notices_sorted if n.opportunity_category == "HIGH"]
    medium = [n for n in notices_sorted if n.opportunity_category == "MEDIUM"]
    low = [n for n in notices_sorted if n.opportunity_category == "LOW"]
    skip = [n for n in notices_sorted if n.opportunity_category == "SKIP"]

    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("email_report.html")

    return template.render(
        date=date_str,
        total_count=len(notices),
        high_opportunities=high,
        medium_opportunities=medium,
        low_opportunities=low,
        skip_opportunities=skip,
        high_count=len(high),
        medium_count=len(medium),
        low_count=len(low),
        skip_count=len(skip),
    )


def generate_email_plain(notices: list[AnalysedNotice], date_str: str = "") -> str:
    """Generate a plain-text fallback of the email."""
    if not date_str:
        date_str = datetime.utcnow().strftime("%d %B %Y")

    lines = [
        f"UK Gazette Insolvency Report – {date_str}",
        f"{'=' * 50}",
        f"Total notices analysed: {len(notices)}",
        "",
    ]

    notices_sorted = sorted(
        notices,
        key=lambda n: (getattr(n, "triage_final", 0) or n.opportunity_score, n.opportunity_score),
        reverse=True,
    )

    for n in notices_sorted:
        tier_tag = f" [LLM {n.llm_tier}]" if getattr(n, "llm_tier", "") and n.llm_tier not in ("unknown", "drop") else ""
        lines.append(f"[{n.opportunity_category}]{tier_tag} {n.company_name} (Score: {n.opportunity_score}/100)")
        if getattr(n, "llm_buyer_hypothesis", ""):
            lines.append(f"  Buyer hypothesis: {n.llm_buyer_hypothesis}")
        elif getattr(n, "llm_why", ""):
            lines.append(f"  LLM: {n.llm_why}")
        if n.company_number:
            lines.append(f"  Company No: {n.company_number}")
        lines.append(f"  Type: {n.notice_type}")
        if n.ch_status:
            lines.append(f"  CH Status: {n.ch_status} | Accounts: {n.ch_accounts_type or 'none'}")
        if n.ch_is_phantom:
            lines.append(f"  *** LIKELY PHANTOM/SHELL ***")
        if n.ch_url:
            lines.append(f"  Companies House: {n.ch_url}")
        if n.ch_filing_history_url:
            lines.append(f"  Filings: {n.ch_filing_history_url}")
        if n.website_url:
            lines.append(f"  Website: {n.website_url}")
        else:
            lines.append(f"  Website: NOT FOUND")
        if n.notice_url:
            lines.append(f"  Gazette: {n.notice_url}")
        if n.registered_address:
            lines.append(f"  Address: {n.registered_address}")
        if n.ch_has_charges:
            charges_str = f"  Charges: {n.ch_total_charges} total"
            if n.ch_outstanding_charges:
                charges_str += f" ({n.ch_outstanding_charges} outstanding)"
            lines.append(charges_str)
        if n.practitioners:
            for p in n.practitioners:
                parts = []
                if p.name:
                    parts.append(p.name)
                if p.role:
                    parts.append(f"({p.role})")
                if p.firm:
                    parts.append(f"at {p.firm}")
                if p.email:
                    parts.append(f"- {p.email}")
                if p.phone:
                    parts.append(f"- {p.phone}")
                lines.append(f"  IP: {' '.join(parts)}")
        if n.ch_recent_filings:
            lines.append(f"  Recent filings ({n.ch_total_filings} total):")
            for f in n.ch_recent_filings[:5]:
                desc = f.description[:80] if f.description else f.filing_type
                lines.append(f"    {f.date} - {desc}")
        if n.opportunity_signals:
            for sig in n.opportunity_signals:
                lines.append(f"  {sig}")
        lines.append("")

    return "\n".join(lines)


def send_email(notices: list[AnalysedNotice]) -> bool:
    """Send the daily email report with PDF attachment. Returns True on success."""
    if not config.SMTP_USER or not config.EMAIL_TO:
        logger.error("SMTP_USER or EMAIL_TO not configured – cannot send email")
        return False

    date_str = datetime.utcnow().strftime("%d %B %Y")
    date_file = datetime.utcnow().strftime("%Y-%m-%d")
    high_count = sum(1 for n in notices if n.opportunity_category == "HIGH")
    l1_count = sum(1 for n in notices if getattr(n, "llm_tier", "") == "L1")
    l2_count = sum(1 for n in notices if getattr(n, "llm_tier", "") == "L2")

    # Em dashes are banned; use commas. Surface the L1/L2 count when LLM triage ran.
    subject = f"Gazette Insolvency Report, {date_str}"
    if l1_count or l2_count:
        subject += f", {l1_count} act, {l2_count} schedule"
    elif high_count:
        subject += f", {high_count} high-potential opportunities"

    # Use mixed multipart to support both alternative content and attachments
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM or config.SMTP_USER
    msg["To"] = config.EMAIL_TO
    if config.EMAIL_CC:
        msg["Cc"] = ", ".join(config.EMAIL_CC)

    # Create alternative part for plain text and HTML
    msg_alternative = MIMEMultipart("alternative")

    # Plain text part
    plain = generate_email_plain(notices, date_str)
    msg_alternative.attach(MIMEText(plain, "plain", "utf-8"))

    # HTML part
    try:
        html = generate_email_html(notices, date_str)
        msg_alternative.attach(MIMEText(html, "html", "utf-8"))
    except Exception as exc:
        logger.warning("Could not render HTML template, sending plain text only: %s", exc)

    msg.attach(msg_alternative)

    # Generate and attach PDF report
    try:
        pdf_bytes = generate_pdf_report(notices, date_str)
        pdf_attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
        pdf_attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"insolvency-report-{date_file}.pdf"
        )
        msg.attach(pdf_attachment)
        logger.info("PDF report attached (%d bytes)", len(pdf_bytes))
    except Exception as exc:
        logger.warning("Could not generate PDF attachment: %s", exc)

    # All recipients
    recipients = [config.EMAIL_TO] + config.EMAIL_CC

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.EMAIL_FROM or config.SMTP_USER, recipients, msg.as_string())
        logger.info("Email sent to %s", ", ".join(recipients))
        return True
    except smtplib.SMTPException as exc:
        logger.error("Failed to send email: %s", exc)
        return False
