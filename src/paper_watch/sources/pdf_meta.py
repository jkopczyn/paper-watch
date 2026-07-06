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
    """{title, abstract} guessed from page-1 text (title = first line)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    title = lines[0]
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
