"""Inspect and manually fix entries that are still just a URL (no real title).

These are the residue the resolvers and the web-search backfill couldn't
identify — a challenge-gated page, a user profile, an ambiguous stub. Use this to
look at them yourself and, where you recognise one, set its title (and optionally
abstract / URL) by hand.

    # list every title-less entry (id, current title, URL, what was said about it)
    uv run python deploy/metadata_repair.py

    # show one entry in full
    uv run python deploy/metadata_repair.py --show 493

    # set a title by hand (optional --abstract / --url / --date); backs up first
    uv run python deploy/metadata_repair.py --set 493 --title "Real Paper Title" \
        --abstract "..." --url "https://arxiv.org/abs/2601.00001" --date 2018-10-01

    # --set without --title keeps the current title (after a y/N confirm) — use it
    # to attach an abstract / url / date to a record whose title is already right
    uv run python deploy/metadata_repair.py --set 373 --url "https://arxiv.org/abs/..."

    # delete a junk entry outright (e.g. a user profile that is not a paper);
    # prompts for confirmation unless --yes is given
    uv run python deploy/metadata_repair.py --delete 493

Unlike the deploy/backfill_*.py scripts, --set and --delete are NOT dry-run: they
write to the live DB immediately (after backing it up to <db>.pre-*.bak).

Recovering a real title can reveal a duplicate; like the resolvers, this merges
the two, so the id you edited may fold into an older one (reported when it does).
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone

from paper_watch.config import Config
from paper_watch.runtime import _entry_lookup_url, _is_titleless, rewrite_paper_metadata
from paper_watch.store import Store


def _titleless_rows(store: Store):
    return [r for r in store.conn.execute("SELECT * FROM entries") if _is_titleless(r)]


def _blurb(store: Store, entry_id: int) -> str:
    return max(
        (m["mention_text"] or "" for m in store.get_mentions(entry_id)),
        key=len,
        default="",
    ).strip()


def cmd_list(store: Store) -> None:
    rows = _titleless_rows(store)
    print(f"{len(rows)} title-less entr{'y' if len(rows) == 1 else 'ies'}:\n")
    for r in rows:
        url = _entry_lookup_url(store, r) or "(no url)"
        blurb = " ".join(_blurb(store, r["id"]).split())[:70]
        print(f"  {r['id']:>4}  {(r['title'] or '')[:34]:34}  {url[:52]}")
        if blurb:
            print(f"        └ said: {blurb}")


def cmd_show(store: Store, entry_id: int) -> None:
    row = store.get_entry(entry_id)
    if row is None:
        sys.exit(f"no entry {entry_id}")
    print(f"id:        {row['id']}")
    print(f"title:     {row['title']!r}")
    print(f"arxiv_id:  {row['arxiv_id']}   doi: {row['doi']}")
    print(f"abstract:  {(row['abstract'] or '(none)')[:200]}")
    print(f"links:     {row['links_json']}")
    print(f"lookup url: {_entry_lookup_url(store, row)}")
    print("mentions:")
    for m in store.get_mentions(entry_id):
        text = " ".join((m["mention_text"] or "").split())[:120]
        print(f"  - {m['source']}  {m['source_item_url']}  {text}")


def _backup(store: Store, tag: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{store.path}.pre-{tag}.{stamp}.bak"
    shutil.copy2(store.path, backup)
    print(f"backed up {store.path} -> {backup}")


def cmd_set(store: Store, args) -> None:
    row = store.get_entry(args.set)
    if row is None:
        sys.exit(f"no entry {args.set}")

    title = args.title if args.title is not None else row["title"]
    if args.title is None:
        # No new title given — keep the current one, but confirm: the caller
        # probably means to attach an abstract / url / date to this record.
        answer = input(
            f"No --title given. Modify entry {args.set} keeping its current title "
            f"{row['title']!r}? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("aborted.")
            return

    _backup(store, "manualtitle")
    # Preserve fields the caller didn't override (only --title/--abstract/--url/
    # --date change anything; authors and an existing abstract are kept).
    authors = json.loads(row["authors_json"])
    abstract = args.abstract if args.abstract is not None else row["abstract"]
    links = {"abstract": args.url} if args.url else {}
    survivor = rewrite_paper_metadata(
        store,
        args.set,
        title=title,
        authors=authors,
        abstract=abstract,
        links=links,
        published_at=args.date,
    )
    if args.title is not None:
        print(f"  {args.set}: {row['title']!r} -> {title!r}")
    else:
        print(f"  {args.set}: title kept as {title!r}")
    if args.url:
        print(f"  url set to {args.url}")
    if args.date:
        print(f"  published_at set to {args.date}")
    if survivor != args.set:
        print(f"  merged into existing twin {survivor}")


def cmd_delete(store: Store, entry_id: int, assume_yes: bool) -> None:
    row = store.get_entry(entry_id)
    if row is None:
        sys.exit(f"no entry {entry_id}")
    n_mentions = len(store.get_mentions(entry_id))
    if not assume_yes:
        answer = input(
            f"Delete entry {entry_id} {row['title']!r} and its {n_mentions} "
            f"mention(s)? A backup is written, but this is otherwise permanent. [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("aborted.")
            return
    _backup(store, "manualdelete")
    # ON DELETE CASCADE (foreign_keys are ON) takes the mentions/metrics/shown/
    # feedback/entry_urls rows with it.
    store.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    store.conn.commit()
    print(f"deleted entry {entry_id}")


def main() -> None:
    p = argparse.ArgumentParser(description="Inspect / fix title-less entries.")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--show", type=int, metavar="ID", help="Show one entry in full.")
    p.add_argument("--set", type=int, metavar="ID", help="Set this entry's title.")
    p.add_argument("--title", help="New title (required with --set).")
    p.add_argument("--abstract", default=None, help="New abstract (optional).")
    p.add_argument("--url", default=None, help="Canonical URL to link (optional).")
    p.add_argument("--date", default=None, help="Publication date, e.g. 2018-10-01 (optional).")
    p.add_argument("--delete", type=int, metavar="ID", help="Delete an entry (prompts first).")
    p.add_argument("--yes", action="store_true", help="Skip the --delete confirmation.")
    args = p.parse_args()

    store = Store(Config.load(args.config).db_path)
    try:
        if args.delete is not None:
            cmd_delete(store, args.delete, args.yes)
        elif args.set is not None:
            cmd_set(store, args)
        elif args.show is not None:
            cmd_show(store, args.show)
        else:
            cmd_list(store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
