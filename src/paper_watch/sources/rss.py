"""RSS/Atom source: pull newsletter and blog feeds from `method-rec.md`.

Newsletter items often link papers in the body; that text is kept for
enrichment context, but ids found in it are *citations*, not the item's own
identity (`extract_ids_from_text=False`) — a newsletter must not adopt the
arXiv id of the first paper it links.
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator

import feedparser

from paper_watch.dates import struct_to_iso
from paper_watch.http import get_text
from paper_watch.models import RawItem
from paper_watch.sources import Fetcher

log = logging.getLogger(__name__)


def _entry_text(entry) -> str | None:
    content = entry.get("content")
    if content:
        return content[0].get("value")
    return entry.get("summary")


def parse_rss(xml: str, feed_name: str) -> list[RawItem]:
    feed = feedparser.parse(xml)
    items: list[RawItem] = []
    for e in feed.entries:
        published = struct_to_iso(
            e.get("published_parsed") or e.get("updated_parsed")
        )
        items.append(
            RawItem(
                source=f"rss:{feed_name}",
                url=e.get("link", ""),
                title=" ".join(e.get("title", "").split()) or None,
                abstract=None,
                text=_entry_text(e),
                published_at=published,
                extract_ids_from_text=False,
            )
        )
    return items


class RssSource:
    name = "rss"

    def __init__(self, feeds: Iterable, fetch: Fetcher = get_text):
        # `feeds` items need `.name` and `.url` (e.g. config.FeedConfig).
        self.feeds = list(feeds)
        self._fetch = fetch

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        for feed in self.feeds:
            try:
                xml = self._fetch(feed.url)
            except Exception as exc:  # one bad feed must not abort the rest
                log.warning("RSS feed failed: %s (%s)", feed.url, exc)
                continue
            for item in parse_rss(xml, feed.name):
                if since and item.published_at and item.published_at < since:
                    continue
                yield item
