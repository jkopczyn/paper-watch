"""One-time backfill: rescale existing relevance from the old 0-4 scale to 0-10.

The relevance rubric moved from 0-4 to 0-10 (finer granularity, gate now at
`relevance >= 4`). Entries scored under the old rubric keep an integer in 0-4;
this maps each onto the new scale (anchored 0->0, 2->5, 4->10, with the old
"1" and "3" bands landing mid-band at 3 and 8). It is a coarse remap -- the old
buckets cannot recover the new granularity, so only future re-enrichments get
it. Gate behaviour on existing rows is unchanged: nothing lands in [4, 5), so
`>= 4` and the old `>= 2`-equivalent (`>= 5`) admit exactly the same rows.

Dry-run by default on a throwaway copy; pass --apply to write (backs up first).
Run from the repo root:

    uv run python deploy/backfill_relevance_scale.py [--apply]
"""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paper_watch.config import Config
from paper_watch.store import Store

# Old 0-4 rubric anchor -> new 0-10 rubric. 0/2/4 are firm anchors (0, 5, 10);
# the old "1" (tangential) and "3" (plausible pick) bands map to their mid-band
# new values (3 and 8).
_RELEVANCE_0_4_TO_0_10 = {0: 0, 1: 3, 2: 5, 3: 8, 4: 10}


def rescale_relevance(old: int) -> int:
    """Map an old 0-4 relevance value onto the new 0-10 scale."""
    if old in _RELEVANCE_0_4_TO_0_10:
        return _RELEVANCE_0_4_TO_0_10[old]
    return max(0, min(10, round(old * 2.5)))  # defensive: unexpected out-of-range


def main(apply: bool) -> None:
    config = Config.load("config.yaml")
    src = config.db_path
    work = src if apply else str(Path(tempfile.mkdtemp()) / "preview.db")

    if apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{src}.pre-relevance-scale.{stamp}.bak"
        shutil.copy2(src, backup)
        print(f"backed up {src} -> {backup}\n")
    else:
        shutil.copy2(src, work)

    store = Store(work)  # opening runs migrations (relevance column already exists)
    rows = store.conn.execute(
        "SELECT id, relevance FROM entries WHERE relevance IS NOT NULL"
    ).fetchall()

    before: dict[int, int] = {}
    after: dict[int, int] = {}
    changed = 0
    for r in rows:
        old = r["relevance"]
        new = rescale_relevance(old)
        before[old] = before.get(old, 0) + 1
        after[new] = after.get(new, 0) + 1
        if new != old:
            store.conn.execute(
                "UPDATE entries SET relevance = ? WHERE id = ?", (new, r["id"])
            )
            changed += 1
    store.conn.commit()

    print(f"relevance histogram before: {dict(sorted(before.items()))}")
    print(f"relevance histogram after:  {dict(sorted(after.items()))}")
    print(
        f"\n{'APPLIED' if apply else 'DRY RUN'}: {changed}/{len(rows)} entries rescaled"
    )
    store.close()
    if not apply:
        shutil.rmtree(Path(work).parent, ignore_errors=True)
        print("re-run with --apply to write")


if __name__ == "__main__":
    main("--apply" in sys.argv)
