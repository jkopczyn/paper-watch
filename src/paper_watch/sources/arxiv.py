"""arXiv source: query the export API by author and yield RawItems.

This replaces the Google Scholar alerts in `method-rec.md`: the configured author
names are the high-precision whitelist, and arXiv has a clean API.

arXiv asks for no more than ~1 request every 3 seconds. With ~50 authors, one
request per author burst-fires past that and gets 429'd, so we batch several
authors into a single `au:"X" OR au:"Y"` query and pause between batches.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from itertools import batched
from typing import Callable, Iterator

import feedparser

from paper_watch.http import get_text
from paper_watch.models import RawItem
from paper_watch.sources import Fetcher

log = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"


def authors_query_url(authors: list[str], max_results: int = 100) -> str:
    clause = " OR ".join(f'au:"{a}"' for a in authors)
    params = urllib.parse.urlencode(
        {
            "search_query": clause,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        }
    )
    return f"{ARXIV_API}?{params}"


def author_query_url(author: str, max_results: int = 50) -> str:
    """Single-author query (kept for callers/tests that want one name)."""
    return authors_query_url([author], max_results)


def parse_arxiv_atom(xml: str) -> list[RawItem]:
    feed = feedparser.parse(xml)
    items: list[RawItem] = []
    for e in feed.entries:
        pdf_url = None
        for link in e.get("links", []):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")
        title = " ".join(e.get("title", "").split())
        authors = [a.get("name") for a in e.get("authors", []) if a.get("name")]
        items.append(
            RawItem(
                source="arxiv",
                url=e.get("link") or e.get("id", ""),
                title=title,
                authors=authors,
                abstract=e.get("summary"),
                pdf_url=pdf_url,
                published_at=e.get("published"),
            )
        )
    return items


class ArxivSource:
    name = "arxiv"

    def __init__(
        self,
        authors: list[str],
        fetch: Fetcher = get_text,
        max_results_per_query: int = 100,
        batch_size: int = 8,
        min_interval: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.authors = authors
        self._fetch = fetch
        self.max_results_per_query = max_results_per_query
        self.batch_size = batch_size
        self.min_interval = min_interval
        self._sleep = sleep

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        for i, batch in enumerate(batched(self.authors, self.batch_size)):
            if i > 0:
                self._sleep(self.min_interval)  # be polite between requests
            url = authors_query_url(list(batch), self.max_results_per_query)
            try:
                xml = self._fetch(url)
            except Exception as exc:  # a failing batch must not abort the run
                log.warning("arxiv batch %s failed: %s", list(batch), exc)
                continue
            for item in parse_arxiv_atom(xml):
                if since and item.published_at and item.published_at < since:
                    continue
                yield item
