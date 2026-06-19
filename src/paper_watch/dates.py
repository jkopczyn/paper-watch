"""Date helpers. We standardize on ISO-8601 UTC strings ('...Z') everywhere so
published timestamps from different sources compare lexicographically."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

_WINDOW = re.compile(r"^\s*(\d+)\s*([dhw])\s*$", re.IGNORECASE)
_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7}


def struct_to_iso(st: time.struct_time | None) -> str | None:
    """Convert a feedparser UTC struct_time to an ISO-8601 'Z' string."""
    if st is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", st)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def since_to_iso(window: str, *, now: datetime | None = None) -> str:
    """Convert a relative window like '7d', '12h', '2w' to an absolute ISO cutoff.

    An exact ISO-8601 string is passed through unchanged.
    """
    m = _WINDOW.match(window)
    if not m:
        return window  # assume already an ISO timestamp
    amount, unit = int(m.group(1)), m.group(2).lower()
    base = now or datetime.now(timezone.utc)
    cutoff = base - timedelta(hours=amount * _UNIT_HOURS[unit])
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
