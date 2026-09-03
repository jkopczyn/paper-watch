"""One-time backfill: fill in the publication dates the new date pass can find.

This supersedes `deploy/backfill_pubdates_pages.py` by adding the two paths that
script does not have: the date stated in a URL path (free, no fetch) and the
LessWrong/AF GraphQL backend, whose pages a plain HTTP metadata fetch cannot
read. It re-runs `runtime.resolve_missing_dates` over every entry with a NULL
`published_at`, newest first, and writes ONLY `published_at` (title / authors /
abstract are left untouched). Sharing the runtime function means the backfill
respects the same per-entry attempt cap the live pass does.

Deterministic by default (URL path, GraphQL, HTML date meta / JSON-LD, PDF
CreationDate). Pass --llm to also use the Claude date fallback for pages that
carry no date metadata; it needs ANTHROPIC_API_KEY and uses `llm.date_model`.
Pass --limit N to cap a trial run.

`resolve_missing_dates` prints nothing per entry, so this script reports totals
by method rather than one line per entry. If per-entry output is wanted, add a
small `on_result` callback to the runtime function rather than duplicating its
loop here.

Dry-run by default on a throwaway copy; pass --apply to write (backs up first).
Run from the repo root:

    uv run python deploy/backfill_pubdates_v2.py [--llm] [--limit N] [--apply]
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from paper_watch import runtime
from paper_watch.config import Config
from paper_watch.sources.html_meta import HtmlMetaResolver
from paper_watch.sources.lesswrong import LessWrongResolver
from paper_watch.sources.pdf_meta import PdfMetaResolver
from paper_watch.store import Store


def summary_lines(result) -> list[str]:
    """The per-method counts of one `resolve_missing_dates` run, as printable lines."""
    return [
        f"  URL path : {result.url}",
        f"  GraphQL  : {result.graphql}",
        f"  HTML meta: {result.html_meta}",
        f"  PDF meta : {result.pdf_meta}",
        f"  LLM      : {result.llm}",
        f"  filled   : {result.total_filled}",
        f"  unfilled : {result.unfilled}",
    ]


def _limit_arg(argv: list[str]) -> int:
    if "--limit" in argv:
        return int(argv[argv.index("--limit") + 1])
    return 10**6  # well above the table size: cover the whole backlog


def main() -> None:
    apply = "--apply" in sys.argv
    use_llm = "--llm" in sys.argv
    limit = _limit_arg(sys.argv)
    config = Config.load("config.yaml")

    src = config.db_path
    work = src if apply else str(Path(tempfile.mkdtemp()) / "preview.db")
    if apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{src}.pre-pubdates-v2.{stamp}.bak"
        shutil.copy2(src, backup)
        print(f"backed up {src} -> {backup}\n")
    else:
        shutil.copy2(src, work)

    date_llm = None
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        from paper_watch.sources.date_llm import ClaudeDateExtractor

        date_llm = ClaudeDateExtractor(config.llm.date_model)
    elif use_llm:
        print("(--llm given but no ANTHROPIC_API_KEY; deterministic only)\n")

    store = Store(work)  # opening migrates the date-attempt columns into place
    result = runtime.resolve_missing_dates(
        store,
        limit=limit,
        now_iso=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        lw_resolver=LessWrongResolver(),
        html_resolver=HtmlMetaResolver(date_llm=date_llm),
        pdf_resolver=PdfMetaResolver(date_llm=date_llm),
    )

    print("\n".join(summary_lines(result)))
    print(f"\n{'APPLIED' if apply else 'DRY RUN'}: {result.total_filled} dates set")
    store.close()
    if not apply:
        shutil.rmtree(Path(work).parent, ignore_errors=True)
        print("re-run with --apply to write")


if __name__ == "__main__":
    main()
