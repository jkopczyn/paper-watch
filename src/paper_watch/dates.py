"""Date helpers. We standardize on ISO-8601 UTC strings ('...Z') everywhere so
published timestamps from different sources compare lexicographically."""

from __future__ import annotations

import time


def struct_to_iso(st: time.struct_time | None) -> str | None:
    """Convert a feedparser UTC struct_time to an ISO-8601 'Z' string."""
    if st is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", st)
