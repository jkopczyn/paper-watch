"""One-time cleanup #2 for the 2026-08-06 trusted-page noise (bugfixes branch).

The first cleanup (cleanup_apollo_and_af_dup.py) removed the Apollo nav flood;
a replay of the 08-07 digest then exposed what had been ranked just below it:

  - 1062 "Primary source": Apollo's Science index links every post's
    underlying report with that anchor text, and title-matching fused 14
    distinct system-card PDFs into this one entry. Junk as an entry; the real
    documents re-enter cleanly if a source ever links them again ("primary
    source" is now on the non-distinctive title list, so they won't re-fuse).
  - 1079 "Silico Terms of Use" / 1080 "Website Terms of Use": Goodfire's
    trusted research page grew /legal/ footer links. The gate now drops
    trusted relevance-0 items, but these predate that fix.

All three URLs stay in their pages' baselines, so nothing re-ingests them.

Dry-run by default; pass --apply to write. Run from the repo root:

    uv run python deploy/cleanup_trusted_junk.py [--apply]
"""

import shutil
import sys
from datetime import datetime, timezone

from paper_watch.config import Config
from paper_watch.store import Store

JUNK_IDS = [1062, 1079, 1080]

apply = "--apply" in sys.argv
config = Config.load("config.yaml")

if apply:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{config.db_path}.pre-trusted-junk-cleanup.{stamp}.bak"
    shutil.copy2(config.db_path, backup)
    print(f"backed up {config.db_path} -> {backup}\n")

store = Store(config.db_path)
placeholders = ",".join("?" * len(JUNK_IDS))

print("junk entries to delete:")
for row in store.conn.execute(
    f"SELECT id, title FROM entries WHERE id IN ({placeholders})", JUNK_IDS
):
    print(f"  {row['id']}: {row['title']}")

if not apply:
    print("\ndry run — pass --apply to write.")
    sys.exit(0)

store.conn.execute(f"DELETE FROM entries WHERE id IN ({placeholders})", JUNK_IDS)
store.conn.commit()
left = store.conn.execute(
    f"SELECT COUNT(*) FROM entries WHERE id IN ({placeholders})", JUNK_IDS
).fetchone()[0]
print(f"\ndone. leftover targeted entries: {left}")
store.close()
