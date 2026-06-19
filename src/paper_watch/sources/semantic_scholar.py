"""Semantic Scholar client for citation counts (used by velocity scoring)."""

from __future__ import annotations

import json

from paper_watch.http import get_text
from paper_watch.sources import Fetcher

S2_API = "https://api.semanticscholar.org/graph/v1/paper"


def paper_url(arxiv_id: str, fields: tuple[str, ...] = ("citationCount",)) -> str:
    return f"{S2_API}/arXiv:{arxiv_id}?fields={','.join(fields)}"


def parse_citation_count(text: str) -> int | None:
    data = json.loads(text)
    count = data.get("citationCount")
    return int(count) if count is not None else None


class SemanticScholar:
    def __init__(self, fetch: Fetcher = get_text):
        self._fetch = fetch

    def citation_count(self, arxiv_id: str) -> int | None:
        """Return the citation count for an arXiv paper, or None on any failure.

        The public S2 endpoint is rate-limited and occasionally 404s for brand-new
        papers, so failures are non-fatal by design.
        """
        try:
            text = self._fetch(paper_url(arxiv_id))
        except Exception:
            return None
        try:
            return parse_citation_count(text)
        except (ValueError, KeyError):
            return None
