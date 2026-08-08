"""When a digest is owed.

The pipeline ticks every few hours (see `deploy/systemd/paper-watch.timer`), but
mail only goes out on the *delivery days* — one digest per series of days, sent
on the last day of the series. Every tick asks one question: has a scheduled
delivery moment passed that no successful send has covered yet?

That single question buys three behaviours for free, with no retry state of its
own beyond the `last_sent_at` watermark:

- **Fallback.** A failed noon send leaves the moment uncovered, so the 16:00,
  20:00, ... ticks retry it until one succeeds.
- **Rollover.** The retries do not stop at midnight; an overdue digest keeps
  being owed until it is actually delivered.
- **Collapse.** Several missed deliveries do not queue up into a burst of
  emails. One send moves the watermark past all of them, and each digest covers
  everything since the previous *successful* send.

A tick also asks a second question — should this tick poll the sources at all?
Ingest needs nowhere near the 4-hourly cadence (the timer stays short only for
same-day send retries), so `is_poll_due` gates fetching to roughly once a day
against its own `last_polled_at` watermark, with one coupling to the delivery
schedule: a delivery-due tick polls first when no poll has covered the owed
point yet, so a digest never goes out describing yesterday. A failed send needs
no snapshot machinery — its retries fall inside the gate and rebuild the same
digest from the unchanged DB.

Delivery times are local (noon means noon where the machine is), which is why
every comparison here goes through `astimezone()` rather than fixed offsets —
that keeps noon at noon across a DST change.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

_ISO = "%Y-%m-%dT%H:%M:%SZ"

_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# A week plus a day: enough to find the neighbouring delivery point from any
# instant, whatever subset of weekdays is configured.
_SEARCH_DAYS = 8


def parse_weekdays(names) -> set[int]:
    """Weekday names to Python weekday numbers (Monday == 0)."""
    days = set()
    for name in names:
        key = str(name).strip().lower()
        if key not in _WEEKDAY_NAMES:
            raise ValueError(f"unknown weekday {name!r}")
        days.add(_WEEKDAY_NAMES[key])
    if not days:
        raise ValueError("at least one delivery weekday is required")
    return days


def parse_deliver_at(value: str) -> time:
    """A local "HH:MM" delivery time."""
    match = _HHMM.match(str(value).strip())
    if not match:
        raise ValueError(f"delivery time must be HH:MM, got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _local_point(day, at: time) -> datetime:
    """`at` on `day` as a local instant.

    `astimezone()` on a naive datetime reads it as local wall-clock time and
    resolves the offset for *that* date, so this stays correct across DST.
    """
    return datetime.combine(day, at).astimezone()


def last_delivery_at_or_before(
    now: datetime, *, days: set[int], at: time
) -> datetime | None:
    """The most recent scheduled delivery moment no later than `now`."""
    local = now.astimezone()
    for back in range(_SEARCH_DAYS):
        day = (local - timedelta(days=back)).date()
        if day.weekday() not in days:
            continue
        point = _local_point(day, at)
        if point <= local:
            return point
    return None


def next_delivery_after(now: datetime, *, days: set[int], at: time) -> datetime | None:
    """The next scheduled delivery moment strictly after `now`."""
    local = now.astimezone()
    for ahead in range(_SEARCH_DAYS):
        day = (local + timedelta(days=ahead)).date()
        if day.weekday() not in days:
            continue
        point = _local_point(day, at)
        if point > local:
            return point
    return None


# Sources are fetched at most this often on off-schedule ticks; the 4-hourly
# timer exists for send retries, not for ingest cadence.
_POLL_INTERVAL = timedelta(hours=24)


def is_poll_due(
    now: datetime,
    last_polled_at: str | None,
    *,
    delivery_due: bool,
    days: set[int],
    at: time,
) -> bool:
    """Should this tick fetch the sources?

    True when no poll is on record, when the last one is at least 24h old, or
    when a delivery is owed and no poll has happened at/after the owed point —
    the digest should describe the world as of its own delivery time, not
    yesterday's poll. A delivery-due tick whose point *is* covered does not
    re-poll: that is the failed-send retry, which rebuilds the same digest from
    the unchanged DB until either a send succeeds or the 24h gate lapses.
    """
    if not last_polled_at:
        return True
    polled = datetime.strptime(last_polled_at, _ISO).replace(tzinfo=timezone.utc)
    if now - polled >= _POLL_INTERVAL:
        return True
    if delivery_due:
        point = last_delivery_at_or_before(now, days=days, at=at)
        if point is not None and polled < point:
            return True
    return False


def is_delivery_due(
    now: datetime, last_sent_at: str | None, *, days: set[int], at: time
) -> bool:
    """Is a digest owed right now?

    True when a delivery moment has passed that the last successful send did not
    cover. With no send on record (a fresh install) the first tick delivers, so
    the schedule is confirmed working rather than silently waiting days.
    """
    point = last_delivery_at_or_before(now, days=days, at=at)
    if point is None:
        return False
    if not last_sent_at:
        return True
    sent = datetime.strptime(last_sent_at, _ISO).replace(tzinfo=timezone.utc)
    return sent < point
