"""One-time backfill: recover titles for entries that are still just a URL.

After the HTML/PDF/arXiv resolvers ran (and the junk-title backfill), a residual
set of entries remain titled with a bare URL and no abstract — dead links,
scanned PDFs, pages with no Open Graph metadata. This runs each of them through
the Claude web-search resolver to recover a real title (+ snippet/abstract) from
the search index.

Uses the Anthropic API (one web_search call per entry) — needs ANTHROPIC_API_KEY.
`--limit N` bounds how many entries are attempted, to cap cost while verifying.
Dry-run by default on a throwaway copy; `--apply` writes (backs up first).

    uv run python deploy/backfill_webtitles.py [--limit N] [--apply]
"""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from paper_watch.config import Config

# The resolver authenticates with ANTHROPIC_API_KEY, which lives in .env (as the
# main pipeline expects) — load it so the script works without exporting it.
load_dotenv()
from paper_watch.runtime import _is_titleless, recover_titles
from paper_watch.sources.web_search import WebSearchResolver
from paper_watch.store import Store

apply = "--apply" in sys.argv
limit = None
if "--limit" in sys.argv:
    limit = int(sys.argv[sys.argv.index("--limit") + 1])

config = Config.load("config.yaml")
src = config.db_path
work = src if apply else str(Path(tempfile.mkdtemp()) / "preview.db")

if apply:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{src}.pre-webtitles.{stamp}.bak"
    shutil.copy2(src, backup)
    print(f"backed up {src} -> {backup}\n")
else:
    shutil.copy2(src, work)

store = Store(work)
ids = [r["id"] for r in store.conn.execute("SELECT * FROM entries") if _is_titleless(r)]
if limit is not None:
    ids = ids[:limit]
print(f"{len(ids)} URL-only entries to recover; querying web search...\n")

before = {i: store.get_entry(i)["title"] for i in ids}
resolver = WebSearchResolver(config.llm.model)
recover_titles(store, ids, resolver)

fixed = 0
for i in ids:
    row = store.get_entry(i)
    if row is not None and row["title"] != before[i]:
        fixed += 1
        print(f"  {i:>3}  {before[i][:34]!r:38} -> {row['title'][:52]!r}")

print(
    f"\n{'APPLIED' if apply else 'DRY RUN'}: {fixed} titles recovered, "
    f"{len(ids) - fixed} left as-is (web search couldn't identify them)"
)
store.close()
if not apply:
    shutil.rmtree(Path(work).parent, ignore_errors=True)
    print("re-run with --apply to write")
