from datetime import datetime, timezone

from paper_watch.dates import parse_to_iso_date, since_to_iso, struct_to_iso


def test_since_to_iso_relative_windows():
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
    assert since_to_iso("7d", now=now) == "2026-06-12T12:00:00Z"
    assert since_to_iso("12h", now=now) == "2026-06-19T00:00:00Z"
    assert since_to_iso("2w", now=now) == "2026-06-05T12:00:00Z"


def test_since_to_iso_passthrough_for_absolute():
    assert since_to_iso("2026-06-01T00:00:00Z") == "2026-06-01T00:00:00Z"


def test_struct_to_iso_none():
    assert struct_to_iso(None) is None


_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_to_iso_date_iso_forms():
    assert parse_to_iso_date("2019-03-11") == "2019-03-11T00:00:00Z"
    assert parse_to_iso_date("2019-03-11T10:30:00Z") == "2019-03-11T10:30:00Z"
    assert parse_to_iso_date("2019-03-11T10:30:00+00:00") == "2019-03-11T10:30:00Z"


def test_parse_to_iso_date_normalizes_offset_to_utc():
    # -05:00 is 5 hours behind UTC, so the UTC wall-clock is 5 hours later.
    assert parse_to_iso_date("2019-03-11T10:30:00-05:00") == "2019-03-11T15:30:00Z"


def test_parse_to_iso_date_human_forms():
    assert parse_to_iso_date("March 11, 2019") == "2019-03-11T00:00:00Z"
    assert parse_to_iso_date("11 March 2019") == "2019-03-11T00:00:00Z"
    assert parse_to_iso_date("2019/03/11") == "2019-03-11T00:00:00Z"
    # month precision only — day defaults to the 1st
    assert parse_to_iso_date("March 2019") == "2019-03-01T00:00:00Z"


def test_parse_to_iso_date_rejects_junk_and_implausible():
    assert parse_to_iso_date("") is None
    assert parse_to_iso_date(None) is None
    assert parse_to_iso_date("not a date") is None
    # a hallucinated far-future date is rejected
    assert parse_to_iso_date("2999-01-01", now=_NOW) is None
    # a run-time future date (past next-day slack) is rejected
    assert parse_to_iso_date("2026-08-01", now=_NOW) is None


def test_parse_to_iso_date_allows_old_but_plausible():
    # the Clarke 1945 article is old but real; only absurd years are rejected
    assert parse_to_iso_date("1945-10-01", now=_NOW) == "1945-10-01T00:00:00Z"
