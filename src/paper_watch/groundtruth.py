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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from paper_watch.sources.slack import extract_urls, slack_history, ts_to_iso

_MAX_PAGES = 20

# Slack number-emoji, in option order: votes for option i are reactions with
# the i-th name. Extend if a poll ever has more than ten options.
NUM_EMOJI = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "keycap_ten"]


@dataclass
class PollOption:
    week: str  # ISO week of the poll message, e.g. "2026-W27"
    message_ts: str
    option: int  # 1-based, in link order
    emoji: str
    votes: int
    url: str
    context: str  # the message line the link came from (title-ish)


def _iso_week(ts: str) -> str:
    epoch = float(ts)
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _reaction_counts(msg: dict) -> dict[str, int]:
    return {
        r.get("name", ""): int(r.get("count", 0)) for r in msg.get("reactions") or []
    }


def _line_for_url(text: str, url: str) -> str:
    for line in text.splitlines():
        if url in line:
            return " ".join(line.split())[:200]
    return ""


def parse_poll_message(msg: dict, *, min_options: int = 2) -> list[PollOption]:
    """One PollOption per link in a poll-shaped message; [] if not a poll.

    Option order = link order; option i's votes are the reaction count of the
    i-th number emoji (0 when the poll used something else — the human pass
    over the CSV catches those).
    """
    text = msg.get("text") or ""
    urls = list(dict.fromkeys(extract_urls(text)))
    if len(urls) < min_options:
        return []
    reactions = _reaction_counts(msg)
    ts = msg.get("ts", "")
    week = _iso_week(ts) if ts else ""
    options: list[PollOption] = []
    for i, url in enumerate(urls, start=1):
        emoji = NUM_EMOJI[i - 1] if i <= len(NUM_EMOJI) else ""
        options.append(
            PollOption(
                week=week,
                message_ts=ts,
                option=i,
                emoji=emoji,
                votes=reactions.get(emoji, 0),
                url=url,
                context=_line_for_url(text, url),
            )
        )
    return options


def export_groundtruth(
    token: str,
    channel_id: str,
    *,
    oldest: str | None,
    path: str | Path,
    fetch=slack_history,
    min_options: int = 2,
) -> int:
    """Scan a channel's history for poll messages and write the CSV. Returns rows."""
    rows: list[PollOption] = []
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

    rows.sort(key=lambda r: (r.message_ts, r.option))
    path = Path(path)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["week", "message_ts", "option", "emoji", "votes", "url", "context"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "week": r.week,
                    "message_ts": r.message_ts,
                    "option": r.option,
                    "emoji": r.emoji,
                    "votes": r.votes,
                    "url": r.url,
                    "context": r.context,
                }
            )
    return len(rows)


def poll_time_iso(message_ts: str) -> str | None:
    return ts_to_iso(message_ts)
