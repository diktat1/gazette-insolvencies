"""Indian insolvency opportunities module.

Mirrors the UK Gazette pipeline: fetch CIRP / liquidation / auction public
announcements from the IBBI register (free, English), score the opportunity,
and emit AnalysedNotice objects that drop straight into the daily email report.
"""

from src.india.analyser import analyse_india_notices

__all__ = ["analyse_india_notices"]
