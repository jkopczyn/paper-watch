"""Paper identity: extract stable IDs, normalize titles, and dedup entries.

The same paper shows up across arXiv, newsletters, and tweets. We resolve it to a
single `entries` row by arXiv ID, then DOI, then normalized title.
"""

from __future__ import annotations

import re

from paper_watch.store import Store

# New-style: 2406.01234 or 2406.01234v3  (4-digit YYMM + 4-5 digit number)
_ARXIV_NEW = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")
# Old-style: hep-th/9901001 or cs.AI/0701001
_ARXIV_OLD = re.compile(r"\b([a-z][a-z-]+(?:\.[A-Z]{2})?/\d{7})\b")
# DOI per Crossref's recommended pattern.
_DOI = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")
_TITLE_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def extract_arxiv_id(text: str | None) -> str | None:
    """Return the canonical (version-stripped) arXiv ID found in `text`, if any."""
    if not text:
        return None
    m = _ARXIV_NEW.search(text)
    if m:
        return m.group(1)
    m = _ARXIV_OLD.search(text)
    if m:
        return m.group(1)
    return None


def extract_doi(text: str | None) -> str | None:
    """Return the first DOI found in `text`, trailing punctuation stripped."""
    if not text:
        return None
    m = _DOI.search(text)
    if not m:
        return None
    return m.group(1).rstrip(".,;)")


def normalize_title(title: str | None) -> str:
    """Lowercase, drop punctuation, and collapse whitespace for fuzzy matching."""
    if not title:
        return ""
    stripped = _TITLE_PUNCT.sub(" ", title.lower())
    return _WS.sub(" ", stripped).strip()


def resolve_or_create(store: Store, fields: dict) -> tuple[int, bool]:
    """Find the existing entry for `fields`, or create it.

    Match order: arXiv ID, then DOI, then normalized title. Returns
    (entry_id, created).
    """
    arxiv_id = fields.get("arxiv_id")
    doi = fields.get("doi")
    title_norm = fields.get("title_norm") or normalize_title(fields.get("title"))

    existing = None
    if arxiv_id:
        existing = store.get_entry_by_arxiv_id(arxiv_id)
    if existing is None and doi:
        existing = store.get_entry_by_doi(doi)
    if existing is None and title_norm:
        existing = store.get_entry_by_title_norm(title_norm)
    if existing is not None:
        return int(existing["id"]), False

    entry_id = store.insert_entry(
        title=fields["title"],
        title_norm=title_norm,
        first_seen_at=fields["first_seen_at"],
        arxiv_id=arxiv_id,
        doi=doi,
        authors=fields.get("authors") or [],
        abstract=fields.get("abstract"),
        links=fields.get("links") or {},
    )
    return entry_id, True
