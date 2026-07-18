"""LLM enrichment: per-paper TL;DR, why-priority line, controlled tags, and a
0-10 relevance score against the reader profile.

The LLM is used ONLY at enrichment time, never per-run at ranking time.
Enrichment is cached on the entry and versioned (`enrich_version`), so each
paper is judged once per schema version; `enrich_unenriched` picks up both
never-enriched entries and entries from older schema versions, capped per run.

The judgment is over abstract + metadata + mention provenance (who shared it
and what they said) — never the paper body.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel

from paper_watch.store import Store

# Bump when the schema/prompt changes enough that old enrichments should be
# redone; entries with a lower version are re-enriched (max_enrich per run).
ENRICH_VERSION = 2

_MAX_MENTION_SNIPPETS = 3
_MENTION_SNIPPET_LEN = 280

RELEVANCE_RUBRIC = """\
Score `relevance` 0-10 against the reader profile (higher = better reading-group fit):
0   = not relevant, or not a research artifact (product news, hiring, chatter)
1-4 = tangential to marginal — real work but a weak fit; score higher within the
      band for a better/more-relevant instance:
      1-2 = only tangentially relevant to the profile
      3   = relevant-adjacent but minor or incremental
      4   = good but not clearly relevant (e.g. an adjacent field), or clearly
            relevant but minor — borderline worth surfacing
5   = squarely in a relevant area, but unlikely reading-group material
6-9 = a plausible reading-group pick; score higher for a stronger, clearer pick:
      6-7 = plausible pick, with some reservation
      8-9 = a strong pick
10  = must-see for this group"""


@dataclass
class EnrichmentResult:
    tldr: str
    why: str  # one line on why this deserves (or doesn't) the reader's attention
    tags: list[str]
    relevance: int  # 0-10 per RELEVANCE_RUBRIC


class Enricher(Protocol):
    def enrich(
        self, *, title: str, abstract: str | None, source: str, mentions: list[str]
    ) -> EnrichmentResult:
        ...


def load_profile(path: str | Path) -> str:
    """Reader-profile text for the prompt; empty (with a fallback line) if absent."""
    p = Path(path)
    if not p.exists():
        return "(no reader profile configured; judge general AI-safety relevance)"
    return p.read_text().strip()


def load_tag_vocabulary(path: str | Path) -> dict[str, str]:
    """tag -> description from tags.yaml; empty dict (= no restriction) if absent."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return dict(data.get("tags") or {})


def mention_snippets(store: Store, entry_id: int) -> list[str]:
    """Provenance lines for the prompt: which source shared it, saying what."""
    out: list[str] = []
    for m in store.get_mentions(entry_id)[:_MAX_MENTION_SNIPPETS]:
        text = " ".join((m["mention_text"] or "").split())[:_MENTION_SNIPPET_LEN]
        out.append(f"{m['source']}: {text}" if text else m["source"])
    return out


def enrich_unenriched(store: Store, enricher: Enricher, limit: int) -> int:
    """Enrich up to `limit` entries lacking current-version enrichment."""
    rows = store.get_unenriched(limit, version=ENRICH_VERSION)
    for row in rows:
        result = enricher.enrich(
            title=row["title"],
            abstract=row["abstract"],
            source=_primary_source(store, row["id"]),
            mentions=mention_snippets(store, row["id"]),
        )
        store.set_enrichment(
            row["id"],
            tldr=result.tldr,
            why=result.why,
            tags=result.tags,
            relevance=result.relevance,
            version=ENRICH_VERSION,
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
    relevance: int


def build_system_prompt(profile: str, vocabulary: dict[str, str]) -> str:
    tag_lines = "\n".join(f"- {tag}: {desc}" for tag, desc in vocabulary.items())
    return (
        "You triage items for an AI-safety reading-group digest, judging only "
        "the title, abstract, and who shared it (never the full text).\n\n"
        f"Reader profile:\n{profile}\n\n"
        f"{RELEVANCE_RUBRIC}\n\n"
        "Also produce:\n"
        "- `tldr`: a one-to-two sentence TL;DR.\n"
        "- `why`: ONE line on why this deserves (or doesn't) the reader's "
        "attention — cite the strongest evidence: the abstract's claim, who "
        "shared it, or where it appeared.\n"
        "- `tags`: 1-4 tags chosen ONLY from this vocabulary:\n"
        f"{tag_lines or '- (no vocabulary configured: use short lowercase tags)'}\n\n"
        "Be terse and concrete."
    )


class ClaudeEnricher:
    """Enricher backed by the Anthropic SDK using structured output."""

    def __init__(
        self,
        model: str,
        client=None,
        *,
        profile: str = "",
        vocabulary: dict[str, str] | None = None,
    ):
        self.model = model
        self.vocabulary = vocabulary or {}
        self._system = build_system_prompt(profile, self.vocabulary)
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def enrich(
        self, *, title: str, abstract: str | None, source: str, mentions: list[str]
    ) -> EnrichmentResult:
        shared = "\n".join(f"- {m}" for m in mentions) or f"- {source}"
        prompt = (
            f"Title: {title}\n\nAbstract:\n{abstract or '(none)'}\n\n"
            f"Shared by:\n{shared}"
        )
        resp = self._client.messages.parse(
            model=self.model,
            max_tokens=1024,
            system=self._system,
            messages=[{"role": "user", "content": prompt}],
            output_format=_LLMEnrichment,
        )
        out = resp.parsed_output
        tags = [t for t in out.tags if not self.vocabulary or t in self.vocabulary]
        return EnrichmentResult(
            tldr=out.tldr,
            why=out.why,
            tags=tags,
            relevance=max(0, min(10, out.relevance)),
        )
