"""One-time backfill: collapse the duplicate entries the pre-`entry_urls` ingest
left behind, and give every surviving entry the URL it was born from.

Before the identity fix, an entry created from a bare PDF link was titled with
its own URL; the resolver then overwrote title_norm with the real title, so the
next run's title lookup missed and created the entry again -- once per run.

Two collapse passes, in order of confidence:

  1. Same source URL. Provably the same item, so this is unconditional. It is
     also the bulk of the damage (the per-run treadmill).
  2. Same distinctive title. Catches one paper reached by several URLs (the AF
     post, the arXiv link, the publisher PDF). Boilerplate titles are skipped --
     two different Anthropic system cards both extract as "System Card", and
     fusing them would lose a paper.

Dry-run by default; pass --apply to write. Run from the repo root.

    uv run python deploy/backfill_dedup.py [--apply]
"""

import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone

from paper_watch.config import Config
from paper_watch.identity import is_distinctive_title
from paper_watch.store import Store

apply = "--apply" in sys.argv
config = Config.load("config.yaml")

if apply:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{config.db_path}.pre-dedup.{stamp}.bak"
    shutil.copy2(config.db_path, backup)
    print(f"backed up {config.db_path} -> {backup}\n")

store = Store(config.db_path)


def entry_url(row) -> str:
    return json.loads(row["links_json"]).get("abstract") or ""


# A dry run must predict what --apply would do, so it tracks the merges it is
# only pretending to make; otherwise pass 2 re-reports the groups pass 1 removed.
gone: set[int] = set()


def collapse(groups: dict, label: str) -> int:
    """Merge each group into its oldest member. Returns entries removed."""
    removed = 0
    for key, ids in sorted(groups.items()):
        live = sorted(i for i in ids if i not in gone)
        if len(live) < 2:
            continue
        winner, losers = live[0], live[1:]
        print(f"  [{label}] keep {winner}, merge {losers}  <- {str(key)[:58]}")
        for loser in losers:
            if apply:
                store.merge_entries(winner_id=winner, loser_id=loser)
            gone.add(loser)
            removed += 1
    return removed


rows = [dict(r) for r in store.conn.execute("SELECT * FROM entries")]
print(f"{len(rows)} entries before\n")

# -- pass 1: identical source URL -------------------------------------------
by_url = defaultdict(list)
for r in rows:
    if url := entry_url(r):
        by_url[url].append(r["id"])
print("pass 1 - same source URL:")
removed_url = collapse(by_url, "url")

# -- pass 2: identical distinctive title -------------------------------------
by_title = defaultdict(list)
for r in rows:
    if r["id"] not in gone and is_distinctive_title(r["title_norm"]):
        by_title[r["title_norm"]].append(r["id"])
print("\npass 2 - same distinctive title:")
removed_title = collapse(by_title, "title")

survivors = [r for r in rows if r["id"] not in gone]
dupe_titles = defaultdict(list)
for r in survivors:
    dupe_titles[r["title_norm"]].append(r["id"])
skipped = {tn: ids for tn, ids in dupe_titles.items() if len(ids) > 1}
if skipped:
    print("\n  left as separate entries (title too generic to prove identity):")
    for tn, ids in sorted(skipped.items()):
        print(f"    {ids} {tn[:56]!r}")

# -- backfill entry_urls on the survivors ------------------------------------
added = 0
for r in survivors:
    if url := entry_url(r):
        if apply:
            store.add_entry_url(r["id"], url)
        added += 1

remaining = len(rows) - len(gone)
print(
    f"\n{'APPLIED' if apply else 'DRY RUN'}: "
    f"{removed_url} merged by url, {removed_title} by title, "
    f"{added} entry_urls seeded, {len(rows)} -> {remaining} entries"
)
if not apply:
    print("re-run with --apply to write")
store.close()
