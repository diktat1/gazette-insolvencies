"""Turkish insolvency opportunities module.

Mirrors the UK Gazette pipeline: fetch bankruptcy-law notices from ilan.gov.tr
(free Basın İlan Kurumu API), enrich the top candidates with the per-ad detail
call, score the opportunity, and emit AnalysedNotice objects that drop straight
into the same daily email report.
"""

from src.turkey.analyser import analyse_turkey_notices

__all__ = ["analyse_turkey_notices"]
