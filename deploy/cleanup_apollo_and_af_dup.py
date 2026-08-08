"""One-time cleanup for the 2026-08-06/07 ingest damage (see bugfixes branch).

Two messes, one script:

  1. Apollo's Webflow rebuild dropped trailing slashes from every href, so the
     whole nav (About, Team, Press, Blog, Careers, the section indexes, cookie
     policy...) diffed as "new posts" and several reached the 2026-08-07 digest
     as Trusted. The page_watch slash-normalization fix stops the recurrence;
     this deletes the junk entries it left behind. The URLs stay in the page
     baselines, so nothing re-ingests them.

  2. The AF feed emitted one post under both alignmentforum.org and
     lesswrong.com hosts, creating two entries (1073/1074) for
     posts/HACauvWhEdC6QhdS4/why-do-models-task-game. The canonicalize_url
     mirror fix stops the recurrence; this merges 1074 into 1073 (which has
     the real title) so the LW URL becomes an alias of the surviving entry.

Dry-run by default; pass --apply to write. Run from the repo root:

    uv run python deploy/cleanup_apollo_and_af_dup.py [--apply]
"""

import shutil
import sys
from datetime import datetime, timezone

from paper_watch.config import Config
from paper_watch.store import Store

# Nav/landing/pagination pages ingested from the Apollo flood, verified by hand:
# Privacy Policy, About, Team, Press, Blog, Science index, Monitoring index,
# Careers, Governance index, Cookie Policy, Contact.
JUNK_IDS = [1002, 1027, 1028, 1029, 1030, 1031, 1032, 1033, 1046, 1047, 1048]
DUP_ID, KEEP_ID = 1074, 1073  # lesswrong-host duplicate -> AF entry w/ real title

apply = "--apply" in sys.argv
config = Config.load("config.yaml")

if apply:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{config.db_path}.pre-apollo-cleanup.{stamp}.bak"
    shutil.copy2(config.db_path, backup)
    print(f"backed up {config.db_path} -> {backup}\n")

store = Store(config.db_path)
conn = store.conn
placeholders = ",".join("?" * len(JUNK_IDS))

print("junk entries to delete:")
for row in conn.execute(
    f"SELECT id, title FROM entries WHERE id IN ({placeholders})", JUNK_IDS
):
    print(f"  {row['id']}: {row['title']}")

dup = conn.execute("SELECT id, title FROM entries WHERE id = ?", (DUP_ID,)).fetchone()
keep = conn.execute("SELECT id, title FROM entries WHERE id = ?", (KEEP_ID,)).fetchone()
if dup and keep:
    print(f"\nmerge {DUP_ID} ({dup['title']!r}) -> {KEEP_ID} ({keep['title']!r})")
else:
    print(f"\nmerge skipped: {DUP_ID} or {KEEP_ID} not present (already cleaned?)")

if not apply:
    print("\ndry run — pass --apply to write.")
    sys.exit(0)

if dup and keep:
    # UNIQUE collisions stay behind on the loser and cascade away with it.
    for table in ("mentions", "entry_urls", "shown", "metrics", "feedback"):
        conn.execute(
            f"UPDATE OR IGNORE {table} SET entry_id = ? WHERE entry_id = ?",
            (KEEP_ID, DUP_ID),
        )
    conn.execute("DELETE FROM entries WHERE id = ?", (DUP_ID,))

conn.execute(f"DELETE FROM entries WHERE id IN ({placeholders})", JUNK_IDS)
conn.commit()

left = conn.execute(
    f"SELECT COUNT(*) FROM entries WHERE id IN ({placeholders}) OR id = ?",
    [*JUNK_IDS, DUP_ID],
).fetchone()[0]
aliases = [
    r["url"] for r in conn.execute("SELECT url FROM entry_urls WHERE entry_id = ?", (KEEP_ID,))
]
print(f"\ndone. leftover targeted entries: {left}")
print(f"entry {KEEP_ID} urls: {aliases}")
store.close()
