"""The public site: every paper paper-watch has put in a digest, with the
reading group's ratings where they exist.

The site is built from the `shown` table, so a paper that never appeared in a
digest never appears here. The internal `score`, the `why` text, the stored
`abstract`, source labels, and everything in `mentions` are never published,
and no field identifies an individual voter: the store holds only per-paper
aggregates. Only http and https links are published; a paper whose first
display link is anything else is shown without a link.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from paper_watch.runtime import _display_links, _pub_display

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def public_url(url: str | None) -> str | None:
    """`url` if it is a plain http or https link, otherwise None.

    This is the gate on everything that reaches an `href`. It rejects
    `javascript:`, `data:` and `slack://` links, protocol-relative URLs, URLs
    with no host, and any string carrying whitespace or a control character.
    The accepted string is returned unchanged, so the published link is the
    one the digest email shows.
    """
    if not url:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    try:
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return url


@dataclass(frozen=True)
class SiteRecord:
    """One appearance of one paper in one digest.

    `entry_id` is internal: it is how the renderers tell two appearances of the
    same paper apart, and `to_json` never serializes it.
    """

    entry_id: int
    digest_at: str
    rank: int | None
    resurfaced: bool
    title: str
    authors: list[str]
    url: str | None
    arxiv_id: str | None
    published: str
    is_estimate: bool
    tags: list[str]
    tldr: str | None
    group_rating: int | None
    picked: bool
    read: bool

    def to_json(self) -> dict:
        """The published form of this record. The key set here is the public
        contract of `papers.json`."""
        return {
            "digest_at": self.digest_at,
            "rank": self.rank,
            "resurfaced": self.resurfaced,
            "title": self.title,
            "authors": list(self.authors),
            "url": self.url,
            "arxiv_id": self.arxiv_id,
            "published": self.published,
            "is_estimate": self.is_estimate,
            "tags": list(self.tags),
            "tldr": self.tldr,
            "group_rating": self.group_rating,
            "picked": self.picked,
            "read": self.read,
        }


def load_records(store) -> list[SiteRecord]:
    """One record per `shown` row, newest digest first and in rank order.

    The displayed link and the publication date come from the same helpers the
    digest email uses, so the site and the email agree on both.
    """
    records: list[SiteRecord] = []
    for row in store.shown_history():
        entry_id = int(row["id"])
        links = _display_links(store, entry_id, json.loads(row["links_json"]))
        first_link = next(iter(links.values()), None)
        published, is_estimate = _pub_display(store, row)
        records.append(
            SiteRecord(
                entry_id=entry_id,
                digest_at=row["digest_at"],
                rank=row["rank"],
                resurfaced=bool(row["resurfaced"]),
                title=row["title"],
                authors=json.loads(row["authors_json"]),
                url=public_url(first_link),
                arxiv_id=row["arxiv_id"],
                published=published,
                is_estimate=is_estimate,
                tags=json.loads(row["tags_json"]),
                tldr=row["tldr"],
                group_rating=row["latest_rating"],
                picked=bool(row["picked"]),
                read=bool(row["read"]),
            )
        )
    return records


def group_by_digest(records: list[SiteRecord]) -> list[tuple[str, list[SiteRecord]]]:
    """`(digest_at, records)` for each digest, keeping the query's order: newest
    digest first, and within a digest the order the email used."""
    groups: list[tuple[str, list[SiteRecord]]] = []
    for record in records:
        if groups and groups[-1][0] == record.digest_at:
            groups[-1][1].append(record)
        else:
            groups.append((record.digest_at, [record]))
    return groups


@dataclass(frozen=True)
class TableRow:
    """One paper in the single table, under the first digest it appeared in."""

    record: SiteRecord
    first_digest_at: str
    times_shown: int


def table_rows(records: list[SiteRecord]) -> list[TableRow]:
    """One row per paper rather than per appearance.

    `record` is the paper's first appearance, so its rank is the rank it had in
    the digest the row is filed under, and `times_shown` counts every
    appearance including that first one. Rows keep the order of the first
    appearances, which is by digest newest first and then by rank.
    """
    first_seen: dict[int, SiteRecord] = {}
    counts: dict[int, int] = {}
    for record in records:
        counts[record.entry_id] = counts.get(record.entry_id, 0) + 1
        # Records are newest first, so the last one seen is the earliest digest.
        first_seen[record.entry_id] = record

    rows = [
        TableRow(
            record=first_seen[record.entry_id],
            first_digest_at=first_seen[record.entry_id].digest_at,
            times_shown=counts[record.entry_id],
        )
        for record in records
        if record is first_seen[record.entry_id]
    ]
    return rows
