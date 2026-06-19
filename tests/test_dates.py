from datetime import datetime, timezone

from paper_watch.dates import since_to_iso, struct_to_iso


def test_since_to_iso_relative_windows():
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
    assert since_to_iso("7d", now=now) == "2026-06-12T12:00:00Z"
    assert since_to_iso("12h", now=now) == "2026-06-19T00:00:00Z"
    assert since_to_iso("2w", now=now) == "2026-06-05T12:00:00Z"


def test_since_to_iso_passthrough_for_absolute():
    assert since_to_iso("2026-06-01T00:00:00Z") == "2026-06-01T00:00:00Z"


def test_struct_to_iso_none():
    assert struct_to_iso(None) is None
