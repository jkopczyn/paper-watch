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


# Human-readable date formats accepted as a fallback when a value is not ISO.
# (ISO-8601 forms are handled directly by datetime.fromisoformat.)
_HUMAN_FORMATS = (
    "%B %d, %Y",  # March 11, 2019
    "%b %d, %Y",  # Mar 11, 2019
    "%d %B %Y",  # 11 March 2019
    "%d %b %Y",  # 11 Mar 2019
    "%Y/%m/%d",  # 2019/03/11
    "%m/%d/%Y",  # 03/11/2019
    "%B %Y",  # March 2019 (day defaults to the 1st)
    "%b %Y",  # Mar 2019
)
_MIN_YEAR = 1900


def _parse_any(raw: str) -> datetime | None:
    """A naive-or-aware datetime from an ISO or common human date string."""
    try:
        # fromisoformat handles date-only, 'Z', and offset forms in 3.11+.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _HUMAN_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_to_iso_date(
    raw: str | None, *, now: datetime | None = None
) -> str | None:
    """Normalize a publication date from any source to an ISO-8601 'Z' string.

    Accepts ISO-8601 (date-only, 'Z', or offset) and common human formats
    ("March 11, 2019", "11 Mar 2019", "2019/03/11"). Offsets are converted to
    UTC. Returns None for anything unparseable or implausible — a year before
    1900, or a date more than a day in the future (a hallucinated/garbage date).
    """
    if not raw or not raw.strip():
        return None
    dt = _parse_any(raw.strip())
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    base = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    if dt.year < _MIN_YEAR or dt > base + timedelta(days=1):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
