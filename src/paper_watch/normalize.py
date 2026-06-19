"""Turn a source's RawItem into the field dict used to create/resolve an entry."""

from __future__ import annotations

from paper_watch.identity import extract_arxiv_id, extract_doi, normalize_title
from paper_watch.models import RawItem

_TITLE_FALLBACK_LEN = 140


def to_entry_fields(raw: RawItem) -> dict:
    """Normalize a RawItem into entry fields.

    Recovers arXiv ID / DOI from explicit fields first, then from the item URL,
    PDF URL, and mention text. When a source gives no title (e.g. a tweet), the
    mention text is used as the title.
    """
    haystack = " ".join(
        part for part in (raw.url, raw.pdf_url, raw.text) if part
    )
    arxiv_id = raw.arxiv_id or extract_arxiv_id(haystack)
    doi = raw.doi or extract_doi(raw.text or "")

    title = raw.title or _title_from_text(raw.text) or raw.url

    links: dict[str, str] = {"abstract": raw.url}
    if raw.pdf_url:
        links["pdf"] = raw.pdf_url
    if raw.code_url:
        links["code"] = raw.code_url

    return {
        "title": title,
        "title_norm": normalize_title(title),
        "arxiv_id": arxiv_id,
        "doi": doi,
        "authors": list(raw.authors),
        "abstract": raw.abstract,
        "links": links,
        "published_at": raw.published_at,
    }


def _title_from_text(text: str | None) -> str | None:
    if not text:
        return None
    first_line = text.strip().splitlines()[0].strip()
    if len(first_line) > _TITLE_FALLBACK_LEN:
        first_line = first_line[:_TITLE_FALLBACK_LEN].rstrip() + "…"
    return first_line or None
