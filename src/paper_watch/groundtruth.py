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
