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
from datetime import datetime, timezone
from pathlib import Path

from paper_watch.score import derive_feedback_keys
from paper_watch.store import Store

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
    entry = store.get_entry(entry_id)
    if entry is None:
        return
    target = (rating - 3) / 2.0  # 1->-1, 3->0, 5->+1
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
