"""Export reading-group poll ground truth from Slack.

The FAR reading-group channel has a weekly message listing ~5 candidate papers;
the group emoji-polls which to read. Each such message becomes ground-truth
rows: one per option, with its vote count — graded human judgment over a
human-preselected candidate set, ideal for scoring the ranker offline.

Poll detection is deliberately loose (any message with >= `min_options` links):
the CSV is meant to be eyeballed and pruned by a human before use.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from paper_watch.sources.slack import extract_urls, slack_history, ts_to_iso

_MAX_PAGES = 20

# An option's ballot emoji is the one prefixing its line
# (":performing_arts: <link|Title>"); votes are reactions with that emoji.
_EMOJI = re.compile(r":([a-z0-9_+'-]+):")


@dataclass
class PollOption:
    week: str  # ISO week of the poll message, e.g. "2026-W27"
    message_ts: str
    option: int  # 1-based, in link order
    emoji: str
    votes: int
    url: str
    context: str  # the message line the link came from (title-ish)
    attendance: int | None = None  # distinct voters in the poll (None if unknown)


def _iso_week(ts: str) -> str:
    epoch = float(ts)
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _reaction_counts(msg: dict) -> dict[str, int]:
    return {
        r.get("name", ""): int(r.get("count", 0)) for r in msg.get("reactions") or []
    }


def _reaction_users(msg: dict) -> dict[str, list[str]]:
    return {r.get("name", ""): list(r.get("users") or []) for r in msg.get("reactions") or []}


def _line_for_url(text: str, url: str) -> str:
    for line in text.splitlines():
        if url in line:
            return " ".join(line.split())[:200]
    return ""


def _ballot_emoji(line: str, url: str) -> str:
    """The emoji labelling this option: last one before the link, else first
    in the line (handles both ':fish: <url>' and '• :one:<url|t>' formats)."""
    head = line.split(url, 1)[0]
    before = _EMOJI.findall(head)
    if before:
        return before[-1]
    anywhere = _EMOJI.findall(line)
    return anywhere[0] if anywhere else ""


def parse_poll_message(msg: dict, *, min_options: int = 2) -> list[PollOption]:
    """One PollOption per link in a poll-shaped message; [] if not a poll.

    Option order = link order. Each option's votes are the reaction count of
    its ballot emoji — the emoji prefixing its line in the message (0 when the
    line has no emoji or nobody reacted; the human pass over the CSV catches
    oddballs).
    """
    text = msg.get("text") or ""
    urls = list(dict.fromkeys(extract_urls(text)))
    if len(urls) < min_options:
        return []
    reactions = _reaction_counts(msg)
    users = _reaction_users(msg)
    ts = msg.get("ts", "")
    week = _iso_week(ts) if ts else ""
    options: list[PollOption] = []
    for i, url in enumerate(urls, start=1):
        line = _line_for_url(text, url)
        emoji = _ballot_emoji(line, url)
        options.append(
            PollOption(
                week=week,
                message_ts=ts,
                option=i,
                emoji=emoji,
                votes=reactions.get(emoji, 0),
                url=url,
                context=line,
            )
        )
    # Turnout: distinct people who reacted with any ballot emoji. Falls back to
    # the top single-option count when the API returned counts but no user lists.
    voters: set[str] = set()
    for o in options:
        voters.update(users.get(o.emoji, []))
    attendance = len(voters) if voters else (max((o.votes for o in options), default=0) or None)
    for o in options:
        o.attendance = attendance
    return options


_FIELDNAMES = [
    "week", "message_ts", "option", "emoji", "votes",
    "attendance", "url", "context",
]


def _existing_rows(path: Path) -> tuple[list[dict], set[str]]:
    """Rows already in the CSV and their message_ts set; ([], set()) if none."""
    if not path.exists() or path.stat().st_size == 0:
        return [], set()
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return rows, {r["message_ts"] for r in rows if r.get("message_ts")}


def changed_polls(path: Path | str, snapshot_path: Path | str) -> set[str]:
    """message_ts of polls hand-edited since the snapshot was taken.

    The snapshot is a copy of the CSV made right after the last successful
    import, so any difference is a hand edit (or hand deletion): a poll counts
    as changed when its row set differs from the snapshot's, including
    vanishing entirely. Polls absent from the snapshot are *new* — ordinary
    append/import territory, not edits. No snapshot yet means no baseline:
    nothing is reported changed.
    """
    snap_rows, snap_ts = _existing_rows(Path(snapshot_path))
    if not snap_ts:
        return set()
    cur_rows, _ = _existing_rows(Path(path))

    def by_poll(rows: list[dict]) -> dict[str, list[tuple]]:
        polls: dict[str, list[tuple]] = {}
        for r in rows:
            ts = r.get("message_ts") or ""
            polls.setdefault(ts, []).append(tuple(sorted(r.items())))
        return {ts: sorted(opts) for ts, opts in polls.items()}

    snap, cur = by_poll(snap_rows), by_poll(cur_rows)
    return {ts for ts in snap if cur.get(ts) != snap[ts]}


def export_groundtruth(
    token: str,
    channel_ids: str | list[str],
    *,
    oldest: str | None,
    path: str | Path,
    fetch=slack_history,
    min_options: int = 2,
    append: bool = False,
) -> int:
    """Scan channels' history for poll messages and write the CSV. Returns rows.

    Accepts one channel id or a list; rows from all channels are merged into a
    single CSV.

    `append` never rewrites existing rows — hand-deletions of misdetected polls
    stick, because covered ranges are not re-fetched: `oldest` becomes the max
    `message_ts` already captured (caller's `oldest` when the file is
    empty/missing), and any fetched poll whose ts is already present is dropped.
    Returns the count of *new* rows only.
    """
    if isinstance(channel_ids, str):
        channel_ids = [channel_ids]
    path = Path(path)
    existing_ts: set[str] = set()
    if append:
        _, existing_ts = _existing_rows(path)
        if existing_ts:
            oldest = max(existing_ts, key=float)
    rows: list[PollOption] = []
    for channel_id in channel_ids:
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = fetch(token, channel_id, oldest, cursor)
            if not page.get("ok", False):
                raise RuntimeError(page.get("error", "slack api error"))
            for msg in page.get("messages", []):
                rows.extend(parse_poll_message(msg, min_options=min_options))
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

    if append:
        # Belt and braces: Slack's `oldest` is exclusive by default, but the
        # dedup keeps behavior independent of that.
        rows = [r for r in rows if r.message_ts not in existing_ts]
    rows.sort(key=lambda r: (r.message_ts, r.option))
    mode = "a" if append else "w"
    file_has_content = path.exists() and path.stat().st_size > 0
    write_header = not (append and file_has_content)
    # Hand edits can strip the final newline; repair before appending so the
    # first new row doesn't glue onto the last existing one.
    if append and file_has_content:
        with path.open("rb+") as fb:
            fb.seek(-1, 2)
            if fb.read(1) not in (b"\n", b"\r"):
                fb.write(b"\r\n")
    with path.open(mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if write_header:
            writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "week": r.week,
                    "message_ts": r.message_ts,
                    "option": r.option,
                    "emoji": r.emoji,
                    "votes": r.votes,
                    "attendance": r.attendance if r.attendance is not None else "",
                    "url": r.url,
                    "context": r.context,
                }
            )
    return len(rows)


def poll_time_iso(message_ts: str) -> str | None:
    return ts_to_iso(message_ts)
