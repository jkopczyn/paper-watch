"""LLM enrichment: per-paper TL;DR, why-relevant blurb, topic tags, and a
safety-relevance gate flag.

The LLM is used ONLY for enrichment here, never for ranking. Enrichment is
cached on the entry (tldr/why/tags/safety_relevant), so each paper is enriched
once; `enrich_unenriched` only touches entries with no tldr yet and caps the
batch per run to control cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from paper_watch.store import Store


@dataclass
class EnrichmentResult:
    tldr: str
    why: str
    tags: list[str]
    safety_relevant: bool


class Enricher(Protocol):
    def enrich(self, *, title: str, abstract: str | None, source: str) -> EnrichmentResult:
        ...


def enrich_unenriched(store: Store, enricher: Enricher, limit: int) -> int:
    """Enrich up to `limit` not-yet-enriched entries. Returns the count enriched."""
    rows = store.get_unenriched(limit)
    for row in rows:
        result = enricher.enrich(
            title=row["title"],
            abstract=row["abstract"],
            source=_primary_source(store, row["id"]),
        )
        store.set_enrichment(
            row["id"],
            tldr=result.tldr,
            why=result.why,
            tags=result.tags,
            safety_relevant=result.safety_relevant,
        )
    return len(rows)


def _primary_source(store: Store, entry_id: int) -> str:
    mentions = store.get_mentions(entry_id)
    return mentions[0]["source"] if mentions else "unknown"


# -- Claude-backed enricher ------------------------------------------------
class _LLMEnrichment(BaseModel):
    tldr: str
    why: str
    tags: list[str]
    safety_relevant: bool


_SYSTEM = (
    "You triage AI-safety research papers for a busy researcher. For each paper, "
    "write a one-to-two sentence TL;DR, a short note on why it is (or isn't) "
    "relevant to AI safety/alignment, 1-4 lowercase topic tags (e.g. interp, "
    "evals, rl, oversight, robustness, agents), and whether it is genuinely "
    "AI-safety-relevant. Be terse and concrete."
)


class ClaudeEnricher:
    """Enricher backed by the Anthropic SDK using structured output."""

    def __init__(self, model: str, client=None):
        self.model = model
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def enrich(self, *, title: str, abstract: str | None, source: str) -> EnrichmentResult:
        prompt = (
            f"Source: {source}\nTitle: {title}\n\nAbstract:\n{abstract or '(none)'}"
        )
        resp = self._client.messages.parse(
            model=self.model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=_LLMEnrichment,
        )
        out = resp.parsed_output
        return EnrichmentResult(
            tldr=out.tldr,
            why=out.why,
            tags=out.tags,
            safety_relevant=out.safety_relevant,
        )
