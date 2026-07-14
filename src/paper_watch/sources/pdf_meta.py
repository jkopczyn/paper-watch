"""Recover a paper's title/abstract from a raw PDF link, page 1 only.

Non-arXiv PDFs (e.g. a lab's `.../paper.pdf`) arrive with no metadata, so the
entry has a URL and nothing to rank or gate on. We fetch the PDF and read its
first page: text extraction (free) for born-digital PDFs, and — only for scanned
image PDFs with no extractable text — a single-page vision OCR call. Never the
body: only page 1 is ever looked at, honoring the "don't digest the paper" rule.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import Callable

from paper_watch.http import get_bytes

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_ABSTRACT = re.compile(r"\babstract\b[:.\s]*", re.IGNORECASE)
_ABSTRACT_END = re.compile(
    r"\n\s*(?:\d+\.?\s+)?introduction\b|\bkeywords\b|\bindex terms\b", re.IGNORECASE
)
_MIN_TEXT_CHARS = 40  # below this, page 1 is effectively image-only → OCR
_MAX_ABSTRACT_CHARS = 2500

# The title is rarely the first line of page 1. Ahead of it sit running heads
# ("Vol.:(0123456789)" on every Springer PDF), journal/volume rules, DOIs,
# section markers and copyright stamps — and on a reprint whose extractable
# first page is really a reference column, there is no title at all. Taking
# line 0 regardless is how "Vol.:(0123456789)" and "48. H.S.Mayberg etal.,
# Ann.Neurol." ended up in the digest as papers.
_FURNITURE = re.compile(
    r"""^(?:
        vol\.?\s*:?\s*\(?[\d\s]+\)?          # Springer running head
      | (?:https?://|doi:|10\.\d{4,9}/)      # DOI / URL line
      | arxiv:\s*\d                          # arXiv stamp
      | \d+\s*\.\s*[A-Z]\s*\.                # numbered reference: "48. H.S.Mayberg"
      | (?:received|accepted|published|submitted|revised)\b
      | (?:\(c\)|©|copyright|downloaded\s+from)\b
      | version\b                            # "Version 3.4.3" doc-version stamp
      | effective\b.*\d{4}                   # "Effective May 26, 2026" date stamp
      | for\s+more\s+information\b           # "For more information, see ..." pointer
      | [\W\d\s]+$                           # digits/punctuation only ("1 3")
    )""",
    re.IGNORECASE | re.VERBOSE,
)
# "Cognition. 40 (1991) l-19", "Minds and Machines (2020) 30:411-437"
_JOURNAL_RULE = re.compile(r"^[A-Z][\w.&\s]{2,40}[.,]?\s*\(?\d{4}\)?\s*[\d:;,()\s\-–—lI]+$")
_CITATION_YEAR = re.compile(r"\(\s*\d{4}\s*\)")
_ET_AL = re.compile(r"\bet\s*al\b", re.IGNORECASE)
# A reprint's extractable "page 1" is sometimes the article's reference column.
# There is no title on such a page, and picking the least citation-shaped line
# out of it just yields a different citation. Judged over content lines only —
# a title page's furniture (DOI, volume rule, "1 3") is numeral-heavy too — and
# only when there are enough of them to mean anything: above the title of a real
# paper sit two or three content lines, above a reference column sit dozens.
_REFERENCE_PAGE_RATIO = 0.6
_MIN_REFERENCE_LINES = 4
_MIN_TITLE_CHARS = 15
_MAX_TITLE_CHARS = 300
_TITLE_SEARCH_LINES = 25


def _is_furniture(line: str) -> bool:
    """Page decoration rather than content: running heads, DOIs, rules, stamps."""
    if _FURNITURE.match(line) or _JOURNAL_RULE.match(line):
        return True
    words = line.split()
    # Short all-caps banners: "GENERAL ARTICLE", "RESEARCH ARTICLE".
    return line.isupper() and len(words) <= 4


def _looks_like_reference(line: str) -> bool:
    """A bibliography entry: dated in parens, "et al.", or numeral-heavy.

    Only the first line of a wrapped reference is numbered, so the numbering is
    not enough on its own — the year and the "et al." survive the wrap.
    """
    if _CITATION_YEAR.search(line) or _ET_AL.search(line):
        return True
    words = line.split()
    if not words:
        return False
    return sum(any(c.isdigit() for c in w) for w in words) * 2 >= len(words)


def _is_title_like(line: str) -> bool:
    if len(line) < _MIN_TITLE_CHARS or _is_furniture(line):
        return False
    return len(line.split()) >= 2 and not _looks_like_reference(line)


def _select_title(lines: list[str]) -> str | None:
    """The paper's title, read off the lines above the abstract.

    Skips furniture to the first title-like line, then absorbs the continuation
    lines a wrapped title leaves behind. Only a lowercase line continues a title:
    the author block that follows it does not ("Iason Gabriel1", "Mark H.
    Johnson"), and that is what stops the run.

    The cost is that a title wrapping onto a capitalised line is truncated —
    "ImageNet Classification with Deep Convolutional" loses "Neural Networks",
    because nothing in the extracted text distinguishes that line from an author's
    name. Truncation is the safe failure: a correct prefix still reads as the
    paper and still ranks, whereas guessing wrong swallows the author list into
    the title. Font sizes would settle it, but page-1 text extraction drops them.
    """
    head = lines[:_TITLE_SEARCH_LINES]
    content = [ln for ln in head if not _is_furniture(ln)]
    if len(content) >= _MIN_REFERENCE_LINES:
        refs = sum(map(_looks_like_reference, content))
        if refs >= len(content) * _REFERENCE_PAGE_RATIO:
            return None

    for i, line in enumerate(head):
        if not _is_title_like(line):
            continue
        title = line
        for cont in lines[i + 1 :]:
            if not cont or cont[0].isupper() or _is_furniture(cont):
                break
            if len(title) + len(cont) + 1 > _MAX_TITLE_CHARS:
                break
            title = f"{title} {cont}"
        return _WS.sub(" ", title).strip()
    return None


def pdf_first_page_text(data: bytes) -> str:
    """Extracted text of the PDF's first page ('' if none / unreadable)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        if not reader.pages:
            return ""
        return reader.pages[0].extract_text() or ""
    except Exception as exc:  # malformed/encrypted PDF
        log.debug("pdf text extraction failed: %s", exc)
        return ""


def pdf_first_page_pdf(data: bytes) -> bytes:
    """A new single-page PDF holding only page 1 (so OCR never sees the body)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(data))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def parse_first_page_text(text: str) -> dict | None:
    """{title, abstract} read off page-1 text, or None if it carries no title."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    # The title sits above the abstract; nothing below it is a candidate.
    head = lines
    for i, line in enumerate(lines):
        if _ABSTRACT.match(line):
            head = lines[:i]
            break
    title = _select_title(head)
    if title is None:
        return None
    abstract: str | None = None
    m = _ABSTRACT.search(text)
    if m:
        rest = text[m.end() :]
        end = _ABSTRACT_END.search(rest)
        abstract = _WS.sub(" ", rest[: end.start()] if end else rest[:_MAX_ABSTRACT_CHARS]).strip()
    return {"title": title, "abstract": abstract or None}


class PdfMetaResolver:
    def __init__(
        self,
        fetch: Callable[[str], bytes] = get_bytes,
        ocr: Callable[[bytes], dict | None] | None = None,
        *,
        min_text_chars: int = _MIN_TEXT_CHARS,
    ):
        self._fetch = fetch
        self._ocr = ocr
        self._min_text_chars = min_text_chars

    def resolve(self, url: str) -> dict | None:
        """{title, abstract} for a PDF URL, or None. Best-effort; never raises."""
        try:
            data = self._fetch(url)
        except Exception as exc:
            log.debug("PDF fetch failed for %s: %s", url, exc)
            return None
        text = pdf_first_page_text(data)
        if len(text.strip()) >= self._min_text_chars:
            parsed = parse_first_page_text(text)
            if parsed and parsed.get("title"):
                return parsed
        if self._ocr is not None:
            try:
                return self._ocr(pdf_first_page_pdf(data))
            except Exception as exc:
                log.debug("PDF OCR failed for %s: %s", url, exc)
        return None


class ClaudePdfOcr:
    """One-page vision OCR: hand a single-page PDF to Claude, get title+abstract.

    Only invoked for image-only PDFs; one page, one call. Mirrors the enrichment
    client's structured-output usage.
    """

    def __init__(self, model: str, client=None):
        self.model = model
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def __call__(self, page_pdf: bytes) -> dict | None:
        import base64

        from pydantic import BaseModel

        class _PdfMeta(BaseModel):
            title: str
            abstract: str

        resp = self._client.messages.parse(
            model=self.model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.standard_b64encode(page_pdf).decode(),
                            },
                        },
                        {
                            "type": "text",
                            "text": "This is the first page of a research paper. "
                            "Return its title and abstract verbatim.",
                        },
                    ],
                }
            ],
            output_format=_PdfMeta,
        )
        out = resp.parsed_output
        return {"title": out.title, "abstract": out.abstract}
