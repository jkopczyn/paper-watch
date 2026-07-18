"""Reading-group feedback loop (v1: editable CSV).

`export_candidates` writes the papers shown over a window to a CSV. You fill in
`picked` and a 1-5 `group_rating` (the group's approval — the real signal, not
your personal pick), then `import_feedback` records it and nudges per-author /
per-tag / per-source weights via an exponential moving average. Email-reply
parsing is a planned v2 path.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from paper_watch.score import derive_feedback_keys, dynamic_feedback_weight
from paper_watch.store import Store

if TYPE_CHECKING:
    from paper_watch.config import Config

_ISO = "%Y-%m-%dT%H:%M:%SZ"

_FIELDS = ["entry_id", "title", "picked", "group_rating", "notes"]
_TRUTHY = {"yes", "y", "true", "1", "x"}


def export_candidates(store: Store, *, since: str, path: str | Path) -> int:
    """Write shown-since papers to a CSV for the reader to fill in. Returns count."""
    rows = store.entries_shown_since(since)
    path = Path(path)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {"entry_id": r["id"], "title": r["title"], "picked": "", "group_rating": "", "notes": ""}
            )
    return len(rows)


def import_feedback(
    store: Store, *, path: str | Path, week: str, alpha: float = 0.3
) -> int:
    """Import a filled candidates CSV. Records feedback and updates weights.

    A 1-5 `group_rating` is centered to [-1, 1] ((rating - 3) / 2) and blended
    into each of the paper's feedback keys via EMA. Rows with a blank rating are
    still recorded but do not move any weight. Returns rows imported.
    """
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with Path(path).open(newline="") as f:
        for row in csv.DictReader(f):
            entry_id = _parse_int(row.get("entry_id"))
            if entry_id is None:
                continue
            picked = (row.get("picked") or "").strip().lower() in _TRUTHY
            rating = _parse_int(row.get("group_rating"))
            notes = (row.get("notes") or "").strip() or None

            store.record_feedback(
                entry_id=entry_id,
                week=week,
                picked=picked,
                group_rating=rating,
                notes=notes,
                imported_at=now,
            )
            count += 1

            if rating is not None:
                _apply_rating(store, entry_id, rating, alpha)
    return count


def _apply_rating(store: Store, entry_id: int, rating: int, alpha: float) -> None:
    _apply_target(store, entry_id, (rating - 3) / 2.0, alpha)  # 1->-1, 3->0, 5->+1


def _apply_target(store: Store, entry_id: int, target: float, alpha: float) -> None:
    """Blend `target` (in [-1, 1]) into each of the paper's feedback keys via EMA."""
    entry = store.get_entry(entry_id)
    if entry is None:
        return
    authors = json.loads(entry["authors_json"])
    tags = json.loads(entry["tags_json"])
    mentions = store.get_mentions(entry_id)
    source = mentions[0]["source"] if mentions else "unknown"

    for key_type, key_value in derive_feedback_keys(authors, tags, source):
        current = store.get_feedback_weight(key_type, key_value)
        updated = (1 - alpha) * current + alpha * target
        store.set_feedback_weight(key_type, key_value, updated)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# -- votes -> learning signal (see PLAN sketch; constants are tunable) --------
# Target is ~linear in votes with an attendance-dependent slope and neutral
# point (each vote counts for more in a smaller poll; a full sweep of a bigger
# poll is a stronger signal), plus a harsher floor for a lone (single) vote.
_VT_AMP = 1.8  # per-vote slope ~ amp / attendance
_VT_V0_SLOPE = 0.3  # neutral vote-count grows with attendance ...
_VT_V0_BASE = 0.9  # ... as 0.3*attendance + 0.9
_VT_LONE_BASE = -0.5  # single-vote floor at attendance 3 ...
_VT_LONE_SLOPE = -0.125  # ... deepening toward -1.0 as attendance rises


def poll_attendance(votes_in_poll: list[int]) -> float:
    """Estimate turnout from a poll's option vote counts: top + runner-up/3.

    A proxy for the ground-truth files that predate captured attendance: the
    winner's count is a floor on turnout, plus a third of the runner-up for the
    people who backed it instead.
    """
    ordered = sorted(votes_in_poll, reverse=True)
    top = ordered[0] if ordered else 0
    runner = ordered[1] if len(ordered) > 1 else 0
    return top + runner / 3.0


def votes_to_target(votes: int, attendance: float) -> float | None:
    """Map an option's votes (given the poll's attendance) to a target in
    [-1, 1]. Returns None for 0 votes (treated as an error, not a signal)."""
    if votes <= 0:
        return None
    a = max(float(attendance), float(votes))  # an option can't outpoll turnout
    target = (_VT_AMP / a) * (votes - (_VT_V0_SLOPE * a + _VT_V0_BASE))
    if votes == 1:  # a lone vote is minimal engagement; penalize harder
        target = min(target, _VT_LONE_BASE + _VT_LONE_SLOPE * (a - 3))
    return max(-1.0, min(1.0, target))


def _score_scale(target: float, score: float) -> float:
    """Prediction-error scaling on the 0-10 score scale (neutral 5.0): a paper
    the model already rates highly gets a smaller boost but a larger penalty; a
    low-rated one the reverse. Bounded at the rails (no runaway near 0 or 10)."""
    c = max(0.0, min(10.0, score))
    return target * ((10.0 - c) / 5.0 if target >= 0 else c / 5.0)


@dataclass
class VoteImportResult:
    imported: int = 0
    skipped_zero: int = 0
    unresolved: int = 0


def _poll_window(message_ts: str, window_days: int) -> tuple[str, str]:
    """(start, end) ISO strings for the candidate window ending at a poll."""
    end = datetime.fromtimestamp(float(message_ts), tz=timezone.utc)
    return (end - timedelta(days=window_days)).strftime(_ISO), end.strftime(_ISO)


def import_votes(
    store: Store,
    *,
    path: str | Path,
    config: "Config",
    week_filter: str | None = None,
    alpha: float = 0.3,
) -> VoteImportResult:
    """Import a ground-truth votes CSV into the learning loop.

    Resolves each poll option to a DB entry (reusing eval.match_entry), turns its
    votes -- given the poll's turnout -- into a target scaled by the paper's
    current score (prediction-error), and nudges the feedback weights. Weeks are
    processed chronologically so each update reflects feedback learned earlier.
    """
    from paper_watch.eval import load_groundtruth, match_entry, score_entry
    from paper_watch.score import normalize_tracked_authors

    rows = load_groundtruth(path)
    if week_filter is not None:
        rows = [r for r in rows if r.week == week_filter]

    # Per-poll turnout (captured, else proxy) and winner (for `picked`).
    polls: dict[str, list] = {}
    for r in rows:
        polls.setdefault(r.message_ts, []).append(r)
    attendance: dict[str, float] = {}
    winner_votes: dict[str, int] = {}
    for ts, opts in polls.items():
        counts = [o.votes for o in opts]
        captured = [o.attendance for o in opts if o.attendance]
        attendance[ts] = float(captured[0]) if captured else poll_attendance(counts)
        winner_votes[ts] = max(counts) if counts else 0

    result = VoteImportResult()
    # One row per (entry_id, week): keep the highest-vote occurrence.
    best: dict[tuple[int, str], object] = {}
    for r in rows:
        r.entry_id = match_entry(store, r)
        if r.entry_id is None:
            result.unresolved += 1
            continue
        key = (r.entry_id, r.week)
        if key not in best or r.votes > best[key].votes:
            best[key] = r

    weights = config.scoring
    priors = config.source_priors
    tracked = normalize_tracked_authors(config.authors)
    window = config.candidate_window_days
    now = datetime.now(timezone.utc).isoformat()

    for r in sorted(best.values(), key=lambda r: float(r.message_ts)):
        base = votes_to_target(r.votes, attendance[r.message_ts])
        if base is None:
            result.skipped_zero += 1
            continue
        start, end = _poll_window(r.message_ts, window)
        w = weights.model_copy(
            update={"feedback": dynamic_feedback_weight(store.count_feedback_weeks())}
        )
        c = score_entry(
            store, r.entry_id, start=start, end=end, weights=w,
            source_priors=priors, tracked_authors=tracked,
            fb_weights=store.get_feedback_weights(),
        )
        target = _score_scale(base, c)
        rating = max(1, min(5, round(3 + 2 * base)))
        store.record_feedback(
            entry_id=r.entry_id,
            week=r.week,
            picked=(r.votes == winner_votes[r.message_ts]),
            group_rating=rating,
            notes=f"{r.votes}/{attendance[r.message_ts]:.0f} votes (auto)",
            imported_at=now,
        )
        _apply_target(store, r.entry_id, target, alpha)
        result.imported += 1
    return result


def import_file(
    store: Store, *, path: str | Path, week: str | None, config: "Config", alpha: float = 0.3
) -> str:
    """Sniff the CSV header and route to the candidates or votes importer.

    `entry_id` column -> the filled-candidates path (week defaults to this ISO
    week); ground-truth columns -> the real-votes path (week acts as a filter,
    None = all). Returns a human-readable summary line.
    """
    with Path(path).open(newline="") as f:
        header = next(csv.reader(f), [])
    cols = set(header)
    if "entry_id" in cols:
        if week is None:
            iso = date.today().isocalendar()
            week = f"{iso.year}-W{iso.week:02d}"
        n = import_feedback(store, path=path, week=week, alpha=alpha)
        return f"Imported {n} feedback row(s) for {week}"
    if {"message_ts", "votes", "url"} <= cols:
        res = import_votes(store, path=path, config=config, week_filter=week, alpha=alpha)
        return (
            f"Imported {res.imported} vote row(s); skipped {res.skipped_zero} "
            f"(zero votes), {res.unresolved} unresolved"
        )
    raise ValueError(f"unrecognized feedback CSV header: {header}")
