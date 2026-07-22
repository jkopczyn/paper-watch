"""LLM fallback for a page's publication date when no metadata carries one.

The deterministic extractors (HTML date meta / JSON-LD, PDF CreationDate) answer
most pages for free. This is the last resort: hand the model the page's visible
text and ask for the stated publication date. Key-gated in the runtime, exactly
like the PDF vision-OCR fallback — no Anthropic key, no extractor.
"""

from __future__ import annotations

import logging
from typing import Protocol

from paper_watch.dates import parse_to_iso_date

log = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 6000
_SYSTEM = (
    "You are given the text of a web page or the first page of a paper. Identify "
    "its publication date — the date the work was published or posted, never "
    "today's date. Respond with the date as ISO YYYY-MM-DD (a bare year or "
    "year-month is fine if that is all that is stated). If the text states no "
    "explicit publication date, respond with null. Never guess or infer a date "
    "that is not written in the text."
)


class DateExtractor(Protocol):
    def __call__(self, text: str) -> str | None:
        ...


def safe_llm_date(extractor: DateExtractor | None, text: str) -> str | None:
    """Run the extractor over `text` and normalize/validate its answer, or None.

    Best-effort: a missing extractor, empty text, an extractor error, or an
    implausible date (rejected by parse_to_iso_date) all yield None.
    """
    if extractor is None or not text or not text.strip():
        return None
    try:
        return parse_to_iso_date(extractor(text))
    except Exception as exc:  # a failed date call never aborts resolution
        log.debug("llm date extraction failed: %s", exc)
        return None


class ClaudeDateExtractor:
    """Ask Claude for the publication date stated in a page's text.

    Mirrors the enrichment / OCR clients' structured-output usage. Returns the
    raw model string (or None); the caller normalizes it via parse_to_iso_date.
    """

    def __init__(self, model: str, client=None):
        self.model = model
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def __call__(self, text: str) -> str | None:
        from pydantic import BaseModel

        class _Date(BaseModel):
            published_date: str | None

        resp = self._client.messages.parse(
            model=self.model,
            max_tokens=100,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text[:_MAX_TEXT_CHARS]}],
            output_format=_Date,
        )
        return resp.parsed_output.published_date
