"""One bounded retry policy for evidence-provider calls.

Provider failures are not all the same. A throttle or a locked local cache is worth
another attempt; a symbol with no fundamentals data is not. This module tells them apart
and never hides the final error from the caller.
"""

import time

TRANSIENT_HINTS = (
    "ratelimit", "rate limit", "too many requests", "429",
    "database is locked", "timed out", "timeout", "connection reset",
    "connection aborted", "temporarily unavailable", "502", "503", "504",
)
TRANSIENT_TYPES = ("ratelimit", "databaseerror", "operationalerror", "timeouterror",
                   "readtimeout", "connecttimeout", "connectionerror", "chunkedencodingerror")


def transient(exc):
    """True when the same call has a real chance of succeeding again."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return name in TRANSIENT_TYPES or any(hint in name or hint in text for hint in TRANSIENT_HINTS)


def call(fn, *args, attempts=3, backoff=(2, 5), retry_on=None, **kwargs):
    """Call ``fn`` with bounded retries for transient failures.

    ``retry_on`` defaults to :func:`transient`. Anything else, and the last error after
    the attempts run out, is raised to the caller so a caller can record a data reason
    instead of scoring a company on a silently empty result.
    """
    predicate = retry_on or transient
    last = None
    for attempt in range(max(1, attempts)):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # the provider raises many unrelated types
            last = exc
            if attempt + 1 >= attempts or not predicate(exc):
                raise
            time.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise last


def isolate_yfinance_cache():
    """Point yfinance's timezone cache at a per-process directory.

    Its default cache is one SQLite file shared by every process and thread on the runner.
    Concurrent downloads then fail with ``OperationalError: database is locked`` and the
    ticker loses its price history for that run, which is what happened to LNTH.
    """
    try:
        import os
        import tempfile
        from pathlib import Path
        import yfinance as yf
        directory = Path(tempfile.gettempdir()) / f"yf-tz-{os.getpid()}"
        directory.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(directory))
        return directory
    except Exception:  # an older yfinance without the setter must still run
        return None
