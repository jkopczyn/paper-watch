"""One-time backfill: give existing non-arXiv entries their real publication date.

`deploy/backfill_pubdates.py` handled arXiv entries. This is its companion for
everything else — blog posts, lab HTML pages, and raw PDFs — whose
`entries.published_at` is NULL, so the digest estimates their date from the
surfacing date (a 2019 post shared last week reads as "~2026-07").

For each dateless non-arXiv entry it fetches the entry's URL through the same
resolvers the live pipeline uses and writes ONLY the extracted `published_at`
(title / authors / abstract are left untouched). Deterministic by default
(HTML date meta / JSON-LD, PDF CreationDate — free, no network model calls);
pass --llm to also use the Claude date fallback for pages that carry no date
metadata (needs ANTHROPIC_API_KEY, and costs one small call per such entry).

Dry-run by default on a throwaway copy; pass --apply to write (backs up first).
Run from the repo root:

    uv run python deploy/backfill_pubdates_pages.py [--llm] [--apply]
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paper_watch.config import Config
from paper_watch.sources.html_meta import HtmlMetaResolver
from paper_watch.sources.openreview import forum_id
from paper_watch.sources.pdf_meta import PdfMetaResolver
from paper_watch.store import Store


def entry_url(store: Store, row) -> str | None:
    """A URL to resolve this entry from: its abstract link, else a mention URL."""
    url = json.loads(row["links_json"]).get("abstract")
    if url and url.startswith("http"):
        return url
    for m in store.get_mentions(row["id"]):
        if m["source_item_url"] and m["source_item_url"].startswith("http"):
            return m["source_item_url"]
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    use_llm = "--llm" in sys.argv
    config = Config.load("config.yaml")

    src = config.db_path
    work = src if apply else str(Path(tempfile.mkdtemp()) / "preview.db")
    if apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{src}.pre-pubdates-pages.{stamp}.bak"
        shutil.copy2(src, backup)
        print(f"backed up {src} -> {backup}\n")
    else:
        shutil.copy2(src, work)

    date_llm = None
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        from paper_watch.sources.date_llm import ClaudeDateExtractor

        date_llm = ClaudeDateExtractor(config.llm.model)
    elif use_llm:
        print("(--llm given but no ANTHROPIC_API_KEY; deterministic only)\n")
    html_resolver = HtmlMetaResolver(date_llm=date_llm)
    pdf_resolver = PdfMetaResolver(date_llm=date_llm)  # date-only: no OCR

    store = Store(work)  # opening migrates the published_at column into place
    rows = store.conn.execute(
        "SELECT id, title, links_json FROM entries "
        "WHERE arxiv_id IS NULL AND published_at IS NULL"
    ).fetchall()
    print(f"{len(rows)} non-arXiv entries lack a publication date; resolving...\n")

    updated = skipped = 0
    for row in rows:
        url = entry_url(store, row)
        if not url:
            skipped += 1
            continue
        if forum_id(url):
            skipped += 1  # OpenReview carries no readable date
            continue
        resolver = pdf_resolver if url.lower().endswith(".pdf") else html_resolver
        meta = resolver.resolve(url)
        pub = meta.get("published_at") if meta else None
        if not pub:
            skipped += 1
            continue
        store.conn.execute(
            "UPDATE entries SET published_at = ? WHERE id = ?", (pub, row["id"])
        )
        updated += 1
        print(f"  {row['id']:>4}  {pub[:10]}  {(row['title'] or '')[:56]}")
    store.conn.commit()

    print(
        f"\n{'APPLIED' if apply else 'DRY RUN'}: {updated} dates set, "
        f"{skipped} left NULL (no URL / OpenReview / no date found)"
    )
    store.close()
    if not apply:
        shutil.rmtree(Path(work).parent, ignore_errors=True)
        print("re-run with --apply to write")


if __name__ == "__main__":
    main()
