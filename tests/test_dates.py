from datetime import datetime, timezone

from paper_watch.dates import (
    date_from_url,
    parse_to_iso_date,
    since_to_iso,
    struct_to_iso,
)


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
_NOW_2026 = _NOW
_NOW_NAIVE = datetime(2026, 7, 22, 12, 0, 0)


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


def test_date_from_url_reads_dashed_path_date():
    url = "https://example.org/research/2026-05-08-some-post"
    assert date_from_url(url, now=_NOW_2026) == "2026-05-08T00:00:00Z"


def test_date_from_url_reads_slashed_path_date():
    url = "https://example.org/2019/03/11/title/"
    assert date_from_url(url) == "2019-03-11T00:00:00Z"


def test_date_from_url_reads_year_month_path():
    # month precision only — the day defaults to the 1st, as parse_to_iso_date does
    assert date_from_url("https://example.org/2018/07/title") == "2018-07-01T00:00:00Z"


def test_date_from_url_ignores_arxiv_ids():
    assert date_from_url("https://arxiv.org/abs/2608.14825") is None
    assert date_from_url("https://arxiv.org/pdf/2608.14825v2") is None
    assert date_from_url("https://arxiv.org/abs/1706.03762") is None


def test_date_from_url_ignores_bare_numbers():
    assert date_from_url("https://example.org/posts/12345/slug") is None
    assert date_from_url("https://example.org/2026") is None
    assert date_from_url("https://example.org/id/20260508") is None


def test_date_from_url_rejects_implausible_years():
    assert date_from_url("https://example.org/1985/03/11/x") is None
    assert date_from_url("https://example.org/1200-01-01-x") is None


def test_date_from_url_rejects_far_future_dates():
    two_months = "https://example.org/2026/09/21/x"
    two_days = "https://example.org/2026/07/24/x"
    assert date_from_url(two_months, now=_NOW_NAIVE) is None
    assert date_from_url(two_days, now=_NOW_NAIVE) == "2026-07-24T00:00:00Z"


def test_date_from_url_accepts_an_aware_now():
    # datetime(y, m, d) is naive; an aware `now` must not raise on comparison
    url = "https://example.org/2026/07/24/x"
    assert date_from_url(url, now=_NOW_2026) == date_from_url(url, now=_NOW_NAIVE)


def test_date_from_url_accepts_a_naive_now():
    assert date_from_url("https://example.org/2019/03/11/x", now=_NOW_NAIVE) == (
        "2019-03-11T00:00:00Z"
    )


def test_date_from_url_rejects_impossible_calendar_dates():
    assert date_from_url("https://example.org/2019/02/31/x") is None
    assert date_from_url("https://example.org/2019-13-01-x") is None


def test_date_from_url_handles_none_and_non_urls():
    assert date_from_url(None) is None
    assert date_from_url("") is None
    assert date_from_url("not a url at all") is None


def test_date_from_url_ignores_the_query_string():
    assert date_from_url("https://example.org/post?d=2019-03-11") is None
