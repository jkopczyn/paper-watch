"""One-time backfill: give existing arXiv entries their real publication date.

`entries.published_at` is new. Going forward the metadata resolver stamps it
when it fetches a post-shaped entry's arXiv metadata, but entries ingested before
this column existed have it NULL — so the digest estimates their date from the
earliest mention, which is the *surfacing* date, not the paper's. That is exactly
the failure that motivated the feature: a 2018 impossibility-results paper
surfaced last week reads as "~2026-07".

This fetches arXiv metadata for every entry that has an arXiv id and no stored
publication date, and writes the real `published` date onto the entry. Title /
authors / abstract are left untouched (this is a date-only backfill).

Dry-run by default on a throwaway copy; pass --apply to write (backs up first).
Run from the repo root:

    uv run python deploy/backfill_pubdates.py [--apply]
"""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paper_watch.config import Config
from paper_watch.http import get_text
from paper_watch.sources.arxiv import fetch_metadata
from paper_watch.store import Store

apply = "--apply" in sys.argv
config = Config.load("config.yaml")

src = config.db_path
work = src if apply else str(Path(tempfile.mkdtemp()) / "preview.db")

if apply:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{src}.pre-pubdates.{stamp}.bak"
    shutil.copy2(src, backup)
    print(f"backed up {src} -> {backup}\n")
else:
    shutil.copy2(src, work)

store = Store(work)  # opening migrates the published_at column into place
rows = store.conn.execute(
    "SELECT id, arxiv_id, title FROM entries "
    "WHERE arxiv_id IS NOT NULL AND published_at IS NULL"
).fetchall()
by_arxiv = {r["arxiv_id"]: r["id"] for r in rows}
titles = {r["id"]: r["title"] for r in rows}
print(f"{len(by_arxiv)} arXiv entries lack a publication date; fetching...\n")

meta = fetch_metadata(list(by_arxiv), get_text)

updated = 0
for arxiv_id, item in meta.items():
    entry_id = by_arxiv.get(arxiv_id)
    if entry_id is None or not item.published_at:
        continue
    store.conn.execute(
        "UPDATE entries SET published_at = ? WHERE id = ?",
        (item.published_at, entry_id),
    )
    updated += 1
    print(f"  {entry_id:>3}  {item.published_at[:10]}  {titles[entry_id][:56]}")
store.conn.commit()

print(
    f"\n{'APPLIED' if apply else 'DRY RUN'}: {updated} dates set, "
    f"{len(by_arxiv) - updated} left NULL (arXiv had no date / fetch missed)"
)
store.close()
if not apply:
    shutil.rmtree(Path(work).parent, ignore_errors=True)
    print("re-run with --apply to write")
