"""Romanian insolvency opportunities module.

Mirrors the UK Gazette pipeline: fetch BPI bulletin entries (free index via
lege5.ro), enrich each company via ANAF's free public APIs (status +
financials by CUI), score the opportunity, and emit AnalysedNotice objects
that drop straight into the same daily email report.
"""

from src.romania.analyser import analyse_romania_notices

__all__ = ["analyse_romania_notices"]
