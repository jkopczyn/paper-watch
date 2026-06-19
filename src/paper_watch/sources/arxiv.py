"""arXiv source: query the export API per author and yield RawItems.

This replaces the Google Scholar alerts in `method-rec.md`: the configured author
names are the high-precision whitelist, and arXiv has a clean API.
"""

from __future__ import annotations

import urllib.parse
from typing import Iterator

import feedparser

from paper_watch.http import get_text
from paper_watch.models import RawItem
from paper_watch.sources import Fetcher

ARXIV_API = "http://export.arxiv.org/api/query"


def author_query_url(author: str, max_results: int = 50) -> str:
    params = urllib.parse.urlencode(
        {
            "search_query": f'au:"{author}"',
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        }
    )
    return f"{ARXIV_API}?{params}"


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
        max_results_per_author: int = 50,
    ):
        self.authors = authors
        self._fetch = fetch
        self.max_results_per_author = max_results_per_author

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        for author in self.authors:
            url = author_query_url(author, self.max_results_per_author)
            xml = self._fetch(url)
            for item in parse_arxiv_atom(xml):
                if since and item.published_at and item.published_at < since:
                    continue
                yield item
