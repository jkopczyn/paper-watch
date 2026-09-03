"""Date helpers. We standardize on ISO-8601 UTC strings ('...Z') everywhere so
published timestamps from different sources compare lexicographically."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

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


def _naive_utc_now(now: datetime | None) -> datetime:
    """A naive UTC datetime to compare parsed (naive) dates against."""
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is not None:
        base = base.astimezone(timezone.utc)
    return base.replace(tzinfo=None)


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
    base = _naive_utc_now(now)
    if dt.year < _MIN_YEAR or dt > base + timedelta(days=1):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Tighter bounds than parse_to_iso_date's: a date stated in a URL path is a
# strong signal, but a bare digit run that only looks like one is not, so
# anything outside the range a blog URL plausibly carries is dropped.
_URL_MIN_YEAR = 1990
_URL_MAX_FUTURE = timedelta(days=31)
_URL_DASHED = re.compile(r"/(\d{4})-(\d{2})-(\d{2})(?=[-/_.]|$)")
_URL_SLASHED = re.compile(r"/(\d{4})/(\d{2})(?:/(\d{2}))?(?=[/-]|$)")


def date_from_url(url: str | None, *, now: datetime | None = None) -> str | None:
    """Read a publication date stated in a URL path, as an ISO-8601 'Z' string.

    Reads the path only (never the query string or fragment), and costs no
    network call. Deliberately conservative: both patterns require the date's
    separators, so an arXiv id like 2608.14825 or an undelimited run like
    20260508 is not read as a date, and a year outside 1990..(a month ahead)
    is rejected.
    """
    if not url:
        return None
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    base = _naive_utc_now(now)
    for pattern in (_URL_DASHED, _URL_SLASHED):
        m = pattern.search(path)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if m.lastindex == 3 and m.group(3) else 1
        try:
            dt = datetime(year, month, day)
        except ValueError:
            continue
        if year < _URL_MIN_YEAR or dt > base + _URL_MAX_FUTURE:
            continue
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


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
