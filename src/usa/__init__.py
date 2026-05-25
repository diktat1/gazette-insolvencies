"""US corporate-bankruptcy opportunities module.

Mirrors the UK Gazette pipeline: fetch recent Chapter 7/11 filings from
CourtListener (free RECAP archive), score the opportunity, and emit
AnalysedNotice objects that drop straight into the same daily email report.
"""

from src.usa.analyser import analyse_usa_notices

__all__ = ["analyse_usa_notices"]
