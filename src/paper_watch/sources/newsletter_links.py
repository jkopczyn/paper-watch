"""Fan a newsletter item out into the papers it links.

Newsletters (Import AI, ML Safety News, …) are ingested as their own entries,
and Stage 2 deliberately stops them adopting a cited paper's arXiv id (the
identity hijack). But the papers they surface were then invisible. This parses a
newsletter body's HTML, keeps the links that point at papers (the paper-link
allowlist, or a link carrying an arXiv id / DOI, or a `.pdf`), and emits one
`RawItem` per link — each its OWN paper, with the newsletter as provenance.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlsplit

from paper_watch.identity import canonicalize_url, extract_arxiv_id, extract_doi
from paper_watch.models import RawItem

_MAX_LINKS_PER_ITEM = 20
_MAX_TEXT_CHARS = 280


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []  # (href, anchor text)
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href
                self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def _is_paper_link(href: str, domains: list[str]) -> bool:
    host = (urlsplit(href).hostname or "").lower()
    if host and any(host == d or host.endswith("." + d) for d in domains):
        return True
    if extract_arxiv_id(href) or extract_doi(href):
        return True
    return urlsplit(href).path.lower().endswith(".pdf")


def extract_paper_links(raw: RawItem, domains: list[str]) -> list[RawItem]:
    """Paper links in `raw`'s body as their own RawItems (deduped, capped)."""
    if not raw.text:
        return []
    parser = _LinkCollector()
    try:
        parser.feed(raw.text)
    except Exception:
        return []

    seen: set[str] = set()
    items: list[RawItem] = []
    for href, anchor in parser.links:
        if not href.lower().startswith("http") or not _is_paper_link(href, domains):
            continue
        canonical = canonicalize_url(href)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        items.append(
            RawItem(
                source=raw.source,
                url=href,
                mention_url=href,
                title=None,
                text=anchor[:_MAX_TEXT_CHARS] if anchor else None,
                published_at=raw.published_at,
                # Unlike the parent newsletter, THIS item's url IS the paper, so an
                # id in its own text/url legitimately identifies it.
                extract_ids_from_text=True,
            )
        )
        if len(items) >= _MAX_LINKS_PER_ITEM:
            break
    return items
