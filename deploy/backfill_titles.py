"""One-time backfill: re-resolve entries still carrying a junk title.

Before this session there was no HTML metadata resolver, so an entry that linked
an ordinary web page (an Anthropic research post, a lab blog) kept whatever title
it was born with — the raw URL, or a link's anchor text ("announced", "watched",
"idea"). And the PDF parser used to take page-1 boilerplate as the title
("Vol.:(0123456789)"). Both are fixed now; this re-runs the affected entries
through the corrected resolvers.

A junk title is one that is either a bare URL or not distinctive enough to be a
real paper title. Each entry is re-routed by its link type exactly as the live
pipeline routes new entries (arXiv / OpenReview / PDF / HTML), via
resolve_paper_metadata(..., reresolve=True).

Dry-run by default: resolution runs on a throwaway copy of the DB and the title
changes are reported, so nothing is written until you pass --apply. Run from the
repo root.

    uv run python deploy/backfill_titles.py [--apply]
"""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import json

from paper_watch.config import Config
from paper_watch.http import get_text
from paper_watch.identity import is_distinctive_title
from paper_watch.runtime import _build_metadata_resolvers, resolve_paper_metadata
from paper_watch.sources.openreview import forum_id
from paper_watch.store import Store

apply = "--apply" in sys.argv
config = Config.load("config.yaml")


def junk_title_ids(store: Store) -> list[int]:
    rows = store.conn.execute("SELECT id, title, title_norm, links_json FROM entries").fetchall()
    ids = []
    for r in rows:
        if not (r["title"].startswith("http") or not is_distinctive_title(r["title_norm"])):
            continue
        # OpenReview's API is login-gated; those entries have their own deliberate
        # medium-high fallback and are not what the fixed HTML/PDF resolvers address.
        if forum_id(json.loads(r["links_json"]).get("abstract") or ""):
            continue
        ids.append(r["id"])
    return ids


def titles(store: Store, ids: list[int]) -> dict[int, str]:
    q = ",".join("?" * len(ids))
    return {
        r["id"]: r["title"]
        for r in store.conn.execute(f"SELECT id, title FROM entries WHERE id IN ({q})", ids)
    }


orv, pdf, html = _build_metadata_resolvers(config)

src = config.db_path
work = src if apply else str(Path(tempfile.mkdtemp()) / "preview.db")

if apply:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{src}.pre-titles.{stamp}.bak"
    shutil.copy2(src, backup)
    print(f"backed up {src} -> {backup}\n")
else:
    shutil.copy2(src, work)

store = Store(work)
ids = junk_title_ids(store)
before = titles(store, ids)
print(f"{len(ids)} entries carry a junk title; re-resolving...\n")

resolve_paper_metadata(
    store, ids, get_text,
    openreview_resolver=orv, pdf_resolver=pdf, html_resolver=html, reresolve=True,
)

# ids can shrink if a re-resolve merges an entry into a twin; report on survivors.
after = titles(store, [i for i in ids if store.get_entry(i) is not None])
fixed = {i: (before[i], after[i]) for i in after if before[i] != after[i]}
merged = [i for i in ids if store.get_entry(i) is None]

for i, (old, new) in sorted(fixed.items()):
    print(f"  {i:>3}  {old[:34]!r:38} -> {new[:52]!r}")
if merged:
    print(f"\n  {len(merged)} merged into an existing twin: {merged}")

print(
    f"\n{'APPLIED' if apply else 'DRY RUN'}: {len(fixed)} titles fixed, "
    f"{len(merged)} merged, {len(ids) - len(fixed) - len(merged)} unchanged "
    f"(no better title found — left as-is)"
)
store.close()
if not apply:
    shutil.rmtree(Path(work).parent, ignore_errors=True)
    print("re-run with --apply to write")
