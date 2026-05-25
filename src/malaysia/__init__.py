"""Malaysian insolvency opportunities module (credential-gated, OFF by default).

The MdI e-Insolvensi portal is the authoritative source for corporate winding-up
but requires a login, so this module is driven with credentials from the
environment (MALAYSIA_USER / MALAYSIA_PASS) and wired OFF until those are set and
the post-login results layout is confirmed. Emits AnalysedNotice objects into
the same daily email report; fails closed.
"""

from src.malaysia.analyser import analyse_malaysia_notices

__all__ = ["analyse_malaysia_notices"]
