"""One-time backfill on the worktree DB copy:
1. Null arxiv_ids that are citation-hijacks (rss-only entries) or bogus
   (fail the fixed regexes).
2. Fetch real arXiv metadata for post-shaped entries.
3. Re-enrich everything under enrichment v2.
"""
import re, sys
from dotenv import load_dotenv
load_dotenv(".env")  # run from the repo root, next to config.yaml

from paper_watch.identity import _ARXIV_NEW, _ARXIV_OLD
from paper_watch.store import Store
from paper_watch.runtime import resolve_paper_metadata, _build_enricher
from paper_watch.enrich import enrich_unenriched
from paper_watch.config import Config
from paper_watch.http import get_text

config = Config.load("config.yaml")
store = Store(config.db_path)

# -- 1. clean bad arxiv ids -------------------------------------------------
nulled_bogus, nulled_rss = [], []
rows = store.conn.execute("SELECT id, title, arxiv_id FROM entries WHERE arxiv_id IS NOT NULL").fetchall()
for r in rows:
    aid = r["arxiv_id"]
    valid = bool(_ARXIV_NEW.fullmatch(aid) or _ARXIV_OLD.fullmatch(aid))
    srcs = {m["source"] for m in store.get_mentions(r["id"])}
    rss_only = bool(srcs) and all(s.startswith("rss:") for s in srcs)
    if not valid or rss_only:
        store.conn.execute("UPDATE entries SET arxiv_id = NULL WHERE id = ?", (r["id"],))
        (nulled_bogus if not valid else nulled_rss).append((r["id"], aid, r["title"][:50]))
store.conn.commit()
print(f"nulled {len(nulled_bogus)} bogus ids, {len(nulled_rss)} rss-citation ids")
for what, lst in (("bogus", nulled_bogus), ("rss", nulled_rss)):
    for eid, aid, t in lst:
        print(f"  [{what}] {eid} {aid}  {t}")

# -- 2. real paper metadata ---------------------------------------------------
all_ids = [r["id"] for r in store.conn.execute("SELECT id FROM entries").fetchall()]
n = resolve_paper_metadata(store, all_ids, get_text)
print(f"metadata resolved for {n} entries")

# -- 3. re-enrich under v2 ----------------------------------------------------
enricher = _build_enricher(config)
print("enricher:", type(enricher).__name__)
if type(enricher).__name__ == "_PassthroughEnricher":
    sys.exit("no ANTHROPIC_API_KEY; aborting before fake enrichment")
total = 0
while True:
    n = enrich_unenriched(store, enricher, 25)
    total += n
    print(f"  enriched {total}...", flush=True)
    if n == 0:
        break
print(f"re-enriched {total} entries")
store.close()
