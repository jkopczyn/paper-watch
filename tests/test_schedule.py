"""Delivery-window arithmetic: when is a digest owed, and when is it not."""

import time as _time
from datetime import datetime, time, timezone

import pytest

from paper_watch.schedule import (
    is_delivery_due,
    is_poll_due,
    last_delivery_at_or_before,
    next_delivery_after,
    parse_deliver_at,
    parse_weekdays,
)

# Tue + Fri at noon: the WTF series lands Friday, the SSMT series lands Tuesday.
DAYS = {1, 4}
NOON = time(12, 0)


@pytest.fixture
def tz(monkeypatch):
    """Pin the process timezone; delivery times are local, so tests must be too."""

    def _set(name):
        monkeypatch.setenv("TZ", name)
        _time.tzset()

    yield _set
    monkeypatch.undo()
    _time.tzset()


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# 2026-08-05 is a Wednesday, so: Fri 2026-08-07, Tue 2026-08-11, Fri 2026-08-14.


def test_parse_weekdays_accepts_names_case_insensitively():
    assert parse_weekdays(["Tue", "fri"]) == {1, 4}


def test_parse_weekdays_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_weekdays(["funday"])


def test_parse_deliver_at_reads_hh_mm():
    assert parse_deliver_at("12:00") == time(12, 0)
    assert parse_deliver_at("16:30") == time(16, 30)


def test_parse_deliver_at_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_deliver_at("noon")


def test_last_delivery_is_the_most_recent_point_that_has_passed(tz):
    tz("UTC")
    # Friday 13:00 — noon that same day has passed.
    assert last_delivery_at_or_before(utc(2026, 8, 7, 13), days=DAYS, at=NOON) == datetime(
        2026, 8, 7, 12, tzinfo=timezone.utc
    )


def test_before_noon_the_last_delivery_is_the_previous_series(tz):
    tz("UTC")
    # Friday 09:00 — today's noon has NOT passed, so the last point is Tuesday.
    assert last_delivery_at_or_before(utc(2026, 8, 7, 9), days=DAYS, at=NOON) == datetime(
        2026, 8, 4, 12, tzinfo=timezone.utc
    )


def test_due_at_noon_on_the_delivery_day(tz):
    tz("UTC")
    # Last email went out Tuesday; it is now Friday noon.
    assert is_delivery_due(
        utc(2026, 8, 7, 12), "2026-08-04T12:00:00Z", days=DAYS, at=NOON
    )


def test_not_due_before_noon_on_the_delivery_day(tz):
    tz("UTC")
    assert not is_delivery_due(
        utc(2026, 8, 7, 8), "2026-08-04T12:00:00Z", days=DAYS, at=NOON
    )


def test_not_due_on_an_ordinary_day(tz):
    tz("UTC")
    # Wednesday: nothing is owed, the Friday series is still accumulating.
    assert not is_delivery_due(
        utc(2026, 8, 5, 12), "2026-08-04T12:00:00Z", days=DAYS, at=NOON
    )


def test_still_due_at_16_00_when_the_noon_send_failed(tz):
    tz("UTC")
    # No send since Tuesday, so noon's failure leaves Friday still owed.
    for hour in (16, 20):
        assert is_delivery_due(
            utc(2026, 8, 7, hour), "2026-08-04T12:00:00Z", days=DAYS, at=NOON
        )


def test_not_due_once_the_send_succeeded(tz):
    tz("UTC")
    assert not is_delivery_due(
        utc(2026, 8, 7, 16), "2026-08-07T12:03:00Z", days=DAYS, at=NOON
    )


def test_an_overdue_series_keeps_retrying_past_midnight(tz):
    tz("UTC")
    # Friday failed all day; Saturday's 4-hourly ticks still owe the digest.
    assert is_delivery_due(
        utc(2026, 8, 8, 4), "2026-08-04T12:00:00Z", days=DAYS, at=NOON
    )


def test_two_missed_deliveries_collapse_into_one(tz):
    tz("UTC")
    # Nothing sent since the previous Friday: due, and one send clears both —
    # `last_sent_at` then sits after Tuesday noon, so nothing stays owed.
    now = utc(2026, 8, 11, 12)
    assert is_delivery_due(now, "2026-07-31T12:00:00Z", days=DAYS, at=NOON)
    assert not is_delivery_due(now, "2026-08-11T12:00:01Z", days=DAYS, at=NOON)


def test_due_when_nothing_has_ever_been_sent(tz):
    tz("UTC")
    assert is_delivery_due(utc(2026, 8, 5, 9), None, days=DAYS, at=NOON)


def test_next_delivery_skips_to_the_following_series(tz):
    tz("UTC")
    # Wednesday -> Friday noon; just after Friday noon -> Tuesday noon.
    assert next_delivery_after(utc(2026, 8, 5, 9), days=DAYS, at=NOON) == datetime(
        2026, 8, 7, 12, tzinfo=timezone.utc
    )
    assert next_delivery_after(utc(2026, 8, 7, 13), days=DAYS, at=NOON) == datetime(
        2026, 8, 11, 12, tzinfo=timezone.utc
    )


def test_delivery_is_local_noon_not_utc_noon(tz):
    tz("America/New_York")
    # Friday 2026-08-07 noon EDT == 16:00 UTC, so 15:00 UTC is too early.
    assert not is_delivery_due(
        utc(2026, 8, 7, 15), "2026-08-04T16:00:00Z", days=DAYS, at=NOON
    )
    assert is_delivery_due(
        utc(2026, 8, 7, 16), "2026-08-04T16:00:00Z", days=DAYS, at=NOON
    )


# -- the daily poll gate ---------------------------------------------------
# Ticks stay 4-hourly (that is what buys same-day send retries), but sources
# are only fetched about once a day; a tick where a delivery is owed polls
# first unless a poll already covered the owed point.


def test_poll_due_when_never_polled(tz):
    tz("UTC")
    assert is_poll_due(
        utc(2026, 8, 5, 8), None, delivery_due=False, days=DAYS, at=NOON
    )


def test_poll_skipped_within_24h_off_schedule(tz):
    tz("UTC")
    # Wednesday 16:00, polled at 08:00: nothing owed, nothing worth fetching yet.
    assert not is_poll_due(
        utc(2026, 8, 5, 16), "2026-08-05T08:00:00Z", delivery_due=False,
        days=DAYS, at=NOON,
    )


def test_poll_due_after_24h(tz):
    tz("UTC")
    assert is_poll_due(
        utc(2026, 8, 6, 9), "2026-08-05T08:00:00Z", delivery_due=False,
        days=DAYS, at=NOON,
    )


def test_delivery_due_tick_forces_poll_when_owed_point_uncovered(tz):
    tz("UTC")
    # Friday 13:00 with the noon digest owed: the midnight poll predates the
    # owed point, so the digest must not go out on stale content.
    assert is_poll_due(
        utc(2026, 8, 7, 13), "2026-08-07T00:00:00Z", delivery_due=True,
        days=DAYS, at=NOON,
    )


def test_delivery_due_tick_does_not_repoll_when_point_covered(tz):
    tz("UTC")
    # The noon tick polled at 12:05 and its send failed: the 16:00 retry
    # rebuilds the digest from the unchanged DB rather than fetching again.
    assert not is_poll_due(
        utc(2026, 8, 7, 16), "2026-08-07T12:05:00Z", delivery_due=True,
        days=DAYS, at=NOON,
    )


def test_persistent_failure_past_24h_repolls(tz):
    tz("UTC")
    # Still failing by Saturday afternoon: the gate has lapsed, send fresher
    # content rather than yesterday's.
    assert is_poll_due(
        utc(2026, 8, 8, 13), "2026-08-07T12:05:00Z", delivery_due=True,
        days=DAYS, at=NOON,
    )


def test_delivery_stays_at_local_noon_across_a_dst_change(tz):
    tz("America/New_York")
    # 2026-03-06 (Fri, EST, noon == 17:00Z) and 2026-03-10 (Tue, EDT, noon == 16:00Z).
    assert last_delivery_at_or_before(
        utc(2026, 3, 6, 17), days=DAYS, at=NOON
    ) == datetime(2026, 3, 6, 17, tzinfo=timezone.utc)
    assert last_delivery_at_or_before(
        utc(2026, 3, 10, 16), days=DAYS, at=NOON
    ) == datetime(2026, 3, 10, 16, tzinfo=timezone.utc)
