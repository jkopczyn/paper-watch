"""Twitter source via Nitter per-user RSS.

Nitter instances are flaky, so each handle is tried across a list of instances
until one responds; a handle that fails everywhere is skipped, never fatal.

Twitter is noisy, so this source only yields tweets that link a paper (an arXiv
ID or DOI is recoverable) - a cheap, source-specific relevance filter.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterator

import feedparser

from paper_watch.dates import struct_to_iso
from paper_watch.http import get_text
from paper_watch.identity import extract_arxiv_id, extract_doi
from paper_watch.models import RawItem
from paper_watch.sources import Fetcher

log = logging.getLogger(__name__)


def rss_url(instance: str, handle: str) -> str:
    return f"{instance.rstrip('/')}/{handle}/rss"


def parse_nitter(xml: str, handle: str) -> list[RawItem]:
    feed = feedparser.parse(xml)
    items: list[RawItem] = []
    for e in feed.entries:
        title = " ".join(e.get("title", "").split())
        if title.startswith("Pinned:"):
            # Pinned tweets sit atop the feed on every fetch; the tweet already
            # appeared organically when it was posted.
            continue
        body = e.get("summary") or ""
        text = f"{title}\n{body}".strip()
        items.append(
            RawItem(
                source=f"twitter:{handle}",
                url=e.get("link", ""),
                title=None,  # tweets have no paper title; recovered from links
                text=text,
                published_at=struct_to_iso(
                    e.get("published_parsed") or e.get("updated_parsed")
                ),
            )
        )
    return items


def _links_paper(item: RawItem) -> bool:
    haystack = f"{item.text or ''} {item.url}"
    return bool(extract_arxiv_id(haystack) or extract_doi(haystack))


class NitterSource:
    name = "twitter"

    def __init__(
        self,
        handles: list[str],
        instances: list[str],
        fetch: Fetcher = get_text,
        min_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.handles = handles
        self.instances = instances
        self._fetch = fetch
        self.min_interval = min_interval
        self._sleep = sleep

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        for i, handle in enumerate(self.handles):
            if i > 0:
                self._sleep(self.min_interval)  # pace requests; Nitter rate-limits
            xml = self._fetch_with_fallback(handle)
            if xml is None:
                continue
            for item in parse_nitter(xml, handle):
                if since and item.published_at and item.published_at < since:
                    continue
                if not _links_paper(item):
                    continue
                yield item

    def _fetch_with_fallback(self, handle: str) -> str | None:
        for instance in self.instances:
            try:
                return self._fetch(rss_url(instance, handle))
            except Exception as exc:
                log.warning("Nitter %s failed for @%s (%s)", instance, handle, exc)
        log.warning("All Nitter instances failed for @%s; skipping", handle)
        return None
