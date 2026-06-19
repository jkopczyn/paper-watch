"""Shared data types passed between sources, normalization, and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawItem:
    """One item as yielded by a source adapter, before normalization.

    `source` is a stable label like "arxiv", "rss:ML Safety", or
    "twitter:NeelNanda5". `text` is the mention blurb (tweet body, newsletter
    snippet) used to recover arXiv IDs / DOIs when the source doesn't give them.
    """

    source: str
    url: str
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    pdf_url: str | None = None
    code_url: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    published_at: str | None = None
    text: str | None = None
