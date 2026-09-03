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
from collections.abc import Sequence
from dataclasses import dataclass, field
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


def _apply_target(
    store: Store, entry_id: int, target: float, alpha: float
) -> set[tuple[str, str]]:
    """Blend `target` (in [-1, 1]) into each of the paper's feedback keys via
    EMA. Returns the (key_type, key_value) pairs touched."""
    entry = store.get_entry(entry_id)
    if entry is None:
        return set()
    authors = json.loads(entry["authors_json"])
    tags = json.loads(entry["tags_json"])
    mentions = store.get_mentions(entry_id)
    source = mentions[0]["source"] if mentions else "unknown"

    touched: set[tuple[str, str]] = set()
    for key_type, key_value in derive_feedback_keys(authors, tags, source):
        current = store.get_feedback_weight(key_type, key_value)
        updated = (1 - alpha) * current + alpha * target
        store.set_feedback_weight(key_type, key_value, updated)
        touched.add((key_type, key_value))
    return touched


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


# -- votes -> learning signal (constants are tunable) -------------------------
# A nomination that draws at least one other vote is always positive, starting
# from a base bonus B(a) that grows with attendance (drawing a peer's vote in a
# bigger room means more) and interpolating linearly up to +1.0 for a full
# sweep. A lone vote (nominator only) is negative, deepening with attendance.
_VT_NOM_BASE = 0.125  # base bonus B(a) = 0.125 + 0.0375*a ...
_VT_NOM_SLOPE = 0.0375  # ... anchored at B(2)=+0.2, B(10)=+0.5
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
    if votes == 1:  # a lone vote is the nominator alone; a negative signal
        target = _VT_LONE_BASE + _VT_LONE_SLOPE * (a - 3)
    else:
        b = _VT_NOM_BASE + _VT_NOM_SLOPE * a
        if a <= 2:  # only votes == 2 == a after the max() clamp
            target = b
        else:
            target = b + (1 - b) * (votes - 2) / (a - 2)
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
    skipped_existing: int = 0
    unresolved: int = 0
    weeks: list[str] = field(default_factory=list)
    weight_keys_touched: int = 0
    unresolved_urls: list[str] = field(default_factory=list)
    ties: list[str] = field(default_factory=list)  # week labels awaiting a call
    readings_recorded: int = 0
    resolutions_backfilled: int = 0
    reimported: int = 0  # rows of hand-edited polls re-run past the gate


def _poll_window(message_ts: str, window_days: int) -> tuple[str, str]:
    """(start, end) ISO strings for the candidate window ending at a poll."""
    end = datetime.fromtimestamp(float(message_ts), tz=timezone.utc)
    return (end - timedelta(days=window_days)).strftime(_ISO), end.strftime(_ISO)


def _resolve_reading(
    store: Store, url: str, arxiv_id: str | None, title_norm: str | None
) -> int | None:
    """Find the entry a readings-ledger row refers to, by any identity key."""
    from paper_watch.identity import canonicalize_url

    if arxiv_id:
        entry = store.get_entry_by_arxiv_id(arxiv_id)
        if entry is not None:
            return int(entry["id"])
    hit = store.get_entry_id_by_mention_url(canonicalize_url(url))
    if hit is not None:
        return hit
    entry = store.get_entry_by_source_url(canonicalize_url(url))
    if entry is not None:
        return int(entry["id"])
    if title_norm:
        entry = store.get_entry_by_title_norm(title_norm)
        if entry is not None:
            return int(entry["id"])
    return None


def _record_reading_for(store: Store, option, *, recorded_at: str) -> None:
    """Ledger a poll option as read, deriving its identity keys from the row."""
    from paper_watch.identity import (
        extract_arxiv_id,
        is_distinctive_title,
        normalize_title,
    )

    title_norm = normalize_title(option.context)
    store.record_reading(
        week=option.week,
        message_ts=option.message_ts,
        url=option.url,
        arxiv_id=extract_arxiv_id(f"{option.url} {option.context}"),
        title_norm=title_norm if is_distinctive_title(title_norm) else None,
        entry_id=option.entry_id,
        recorded_at=recorded_at,
    )


@dataclass
class TieOption:
    """One tied-top poll option, carrying enough to show a human a choice."""

    title: str
    authors: list[str]
    source: str  # the option URL's host, e.g. "arxiv.org"
    row: object  # the underlying GroundTruthRow (entry_id resolved if possible)


@dataclass
class TiePoll:
    week: str
    message_ts: str
    options: list[TieOption]


def outstanding_ties(store: Store, path: str | Path) -> list[TiePoll]:
    """Tied polls still awaiting a human call, oldest first.

    A poll is outstanding when its top vote count is shared and no readings
    row exists for it yet — resolve_tie writes those rows, so a settled tie
    drops out of this list (and out of the refresh notice).
    """
    from urllib.parse import urlparse

    from paper_watch.eval import load_groundtruth, match_entry

    polls: dict[str, list] = {}
    for r in load_groundtruth(path):
        polls.setdefault(r.message_ts, []).append(r)

    ties: list[TiePoll] = []
    for ts in sorted(polls, key=float):
        opts = polls[ts]
        top = max(o.votes for o in opts)
        if sum(o.votes == top for o in opts) < 2 or store.has_reading_for_poll(ts):
            continue
        options: list[TieOption] = []
        for o in opts:
            if o.votes != top:
                continue
            o.entry_id = match_entry(store, o)
            entry = store.get_entry(o.entry_id) if o.entry_id is not None else None
            options.append(
                TieOption(
                    title=entry["title"] if entry is not None else o.context,
                    authors=json.loads(entry["authors_json"]) if entry is not None else [],
                    source=urlparse(o.url).netloc,
                    row=o,
                )
            )
        ties.append(TiePoll(week=opts[0].week, message_ts=ts, options=options))
    return ties


def parse_tie_choice(raw: str, n_options: int) -> int | list[int]:
    """Read a resolve-ties answer: "0", "N", or a comma-separated list.

    Returns an int for the first two forms and a list of 1-based indices for
    the third. Every rejection raises ValueError carrying a message the CLI
    prints back to the user before re-prompting.
    """
    bad = ValueError(
        f"options are 0 or 1-{n_options}, or a comma-separated list like 1,{n_options}"
    )
    text = (raw or "").strip()
    if not text:
        raise bad
    if "," not in text:
        if not text.isdigit():
            raise bad
        value = int(text)
        if not 0 <= value <= n_options:
            raise bad
        return value
    parts = [p.strip() for p in text.split(",")]
    if not all(p.isdigit() for p in parts):
        raise bad
    values = [int(p) for p in parts]
    if any(not 1 <= v <= n_options for v in values):
        raise bad
    if len(set(values)) != len(values):
        raise bad
    return values


def resolve_tie(
    store: Store, tie: TiePoll, choice: int | Sequence[int], *, recorded_at: str
) -> int:
    """Apply a human call on a tie; returns the readings recorded.

    `choice` takes three forms. N (1-based) marks that option read and picked.
    0 means "all/none/don't remember" — every tied option is marked read
    (exclusion is cheap and safe whichever of those it was) but none is picked.
    A list of two or more indices marks each listed option read and picks
    nobody: several papers were read, so which one the poll settled on is not
    known. Either way the poll gains ledger rows, so it stops counting as
    outstanding.
    """
    if not isinstance(choice, int):
        values = list(choice)
        if not values:
            raise ValueError("choose at least one option")
        if any(not 1 <= v <= len(tie.options) for v in values):
            raise ValueError(f"options are 1-{len(tie.options)}")
        if len(set(values)) != len(values):
            raise ValueError("options must not repeat")
        if len(values) == 1:
            choice = values[0]
        else:
            for v in values:
                _record_reading_for(store, tie.options[v - 1].row, recorded_at=recorded_at)
            return len(values)
    chosen = tie.options if choice == 0 else [tie.options[choice - 1]]
    for opt in chosen:
        _record_reading_for(store, opt.row, recorded_at=recorded_at)
    if choice != 0:
        picked = tie.options[choice - 1].row
        if picked.entry_id is not None:
            store.set_feedback_picked(picked.entry_id, tie.week)
    return len(chosen)


def import_votes(
    store: Store,
    *,
    path: str | Path,
    config: "Config",
    week_filter: str | None = None,
    alpha: float = 0.3,
    force_ts: frozenset[str] | set[str] = frozenset(),
) -> VoteImportResult:
    """Import a ground-truth votes CSV into the learning loop.

    Resolves each poll option to a DB entry (reusing eval.match_entry), turns its
    votes -- given the poll's turnout -- into a target scaled by the paper's
    current score (prediction-error), and nudges the feedback weights. Weeks are
    processed chronologically so each update reflects feedback learned earlier.

    Idempotent: a (entry, week) pair already in `feedback` is skipped entirely,
    so re-running an import (the weekly refresh does) never double-moves the EMA
    weights. Each non-tied poll's winner also enters the readings ledger — even
    when its URL resolves to no entry yet (read before first ingest); those
    unresolved ledger rows are re-resolved at the start of every later import.
    A poll whose top vote count is shared marks nobody picked and enters no
    ledger row; the tie is reported for a human call.

    `force_ts` (polls hand-edited since their import — see
    `groundtruth.changed_polls`) bypasses the gate for those polls: feedback
    rows are re-recorded from the corrected data, the ledger winner re-derived,
    and a poll deleted from the CSV loses its ledger row. One caveat: the EMA
    is order-dependent, so the original nudge is not unwound — the corrected
    target is blended on top and the stale contribution decays.
    """
    from paper_watch.eval import load_groundtruth, match_entry, score_entry
    from paper_watch.score import normalize_tracked_authors

    result = VoteImportResult()
    now = datetime.now(timezone.utc).isoformat()

    # Backfill first: papers read before their first ingest may exist by now.
    for reading in store.unresolved_readings():
        eid = _resolve_reading(
            store, reading["url"], reading["arxiv_id"], reading["title_norm"]
        )
        if eid is not None:
            store.set_reading_entry(reading["id"], eid)
            result.resolutions_backfilled += 1

    # Hand-edited polls: the old ledger rows describe superseded data (a
    # deleted poll's row would otherwise linger forever); the correct winner,
    # if any, is re-recorded below from the fixed rows.
    for ts in force_ts:
        store.delete_readings_for_poll(ts)

    rows = load_groundtruth(path)
    if week_filter is not None:
        rows = [r for r in rows if r.week == week_filter]

    # Per-poll turnout (captured, else proxy), winner (for `picked`), ties.
    polls: dict[str, list] = {}
    for r in rows:
        polls.setdefault(r.message_ts, []).append(r)
    attendance: dict[str, float] = {}
    winner_votes: dict[str, int] = {}
    tied: dict[str, bool] = {}
    for ts, opts in polls.items():
        counts = [o.votes for o in opts]
        captured = [o.attendance for o in opts if o.attendance]
        attendance[ts] = float(captured[0]) if captured else poll_attendance(counts)
        winner_votes[ts] = max(counts) if counts else 0
        tied[ts] = sum(c == winner_votes[ts] for c in counts) > 1

    # One row per (entry_id, week): keep the highest-vote occurrence.
    best: dict[tuple[int, str], object] = {}
    for r in rows:
        r.entry_id = match_entry(store, r)
        if r.entry_id is None:
            result.unresolved += 1
            result.unresolved_urls.append(r.url)
            continue
        key = (r.entry_id, r.week)
        if key not in best or r.votes > best[key].votes:
            best[key] = r

    # Readings ledger: each non-tied poll's winner, resolved or not.
    for ts in sorted(polls, key=float):
        if tied[ts]:
            # A tie a human has since settled (resolve-ties wrote its ledger
            # rows) no longer needs a call; only open ones are reported.
            if not store.has_reading_for_poll(ts):
                result.ties.append(polls[ts][0].week)
            continue
        winner = max(polls[ts], key=lambda o: o.votes)
        if winner.votes <= 0:  # a zero-vote "winner" is a detection error
            continue
        _record_reading_for(store, winner, recorded_at=now)
        result.readings_recorded += 1

    weights = config.scoring
    priors = config.source_priors
    tracked = normalize_tracked_authors(config.authors)
    window = config.candidate_window_days

    weeks: set[str] = set()
    keys_touched: set[tuple[str, str]] = set()
    for r in sorted(best.values(), key=lambda r: float(r.message_ts)):
        base = votes_to_target(r.votes, attendance[r.message_ts])
        if base is None:
            result.skipped_zero += 1
            continue
        forced = r.message_ts in force_ts
        if store.has_feedback(r.entry_id, r.week) and not forced:
            result.skipped_existing += 1
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
            picked=(
                not tied[r.message_ts] and r.votes == winner_votes[r.message_ts]
            ),
            group_rating=rating,
            notes=f"{r.votes}/{attendance[r.message_ts]:.0f} votes (auto)",
            imported_at=now,
        )
        keys_touched |= _apply_target(store, r.entry_id, target, alpha)
        if forced:
            result.reimported += 1
        else:
            result.imported += 1
        weeks.add(r.week)
    result.weeks = sorted(weeks)
    result.weight_keys_touched = len(keys_touched)
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
            f"(zero votes) + {res.skipped_existing} (already imported), "
            f"{res.unresolved} unresolved, {len(res.ties)} tie(s)"
        )
    raise ValueError(f"unrecognized feedback CSV header: {header}")
