"""End-to-end pipeline: ingest -> enrich -> score -> select -> render -> deliver."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_watch.config import Config, ScoringWeights
from paper_watch.dates import since_to_iso
from paper_watch.digest import DigestItem, render_html, score_explanation
from paper_watch.enrich import EnrichmentResult, enrich_unenriched
from paper_watch.identity import canonicalize_url, resolve_or_create
from paper_watch.normalize import to_entry_fields
from paper_watch.score import (
    ScoreFeatures,
    best_source_prior,
    citation_growth,
    compute_score,
    derive_feedback_keys,
    feedback_affinity,
    has_tracked_author,
    normalize_tracked_authors,
)

_ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class RunResult:
    chosen_ids: list[int] = field(default_factory=list)
    digest_path: Path | None = None
    sent: bool = False
    new_count: int = 0
    enriched_count: int = 0


# -- ingest ----------------------------------------------------------------
def ingest(store, sources, since: str | None, now_iso: str) -> list[int]:
    """Fetch every source, normalize, dedup into entries, and record mentions.

    Returns the ids of entries newly created this run.
    """
    new_ids: list[int] = []
    for source in sources:
        for raw in source.fetch(since):
            canonical = canonicalize_url(raw.url)
            if canonical != raw.url:
                raw = replace(raw, url=canonical)
            fields = to_entry_fields(raw)
            fields["first_seen_at"] = now_iso
            entry_id, created = resolve_or_create(store, fields)
            if created:
                new_ids.append(entry_id)
            store.add_mention(
                entry_id=entry_id,
                source=raw.source,
                source_item_url=canonicalize_url(raw.mention_url) or raw.url,
                mention_text=raw.text,
                published_at=fields.get("published_at"),
                fetched_at=now_iso,
                trusted=raw.trusted,
            )
    return new_ids


def resolve_paper_metadata(store, entry_ids: list[int], fetch) -> int:
    """Give post-shaped entries their real paper metadata, best-effort.

    A tweet/Slack entry that links an arXiv id is created with the post text as
    its title and no abstract; the LLM gate and any content-based ranking need
    the actual paper. Batch-fetches the arXiv API for entries that have an
    arxiv_id but no abstract. Returns how many entries were updated.
    """
    from paper_watch.identity import normalize_title
    from paper_watch.sources.arxiv import fetch_metadata

    pending: dict[str, int] = {}
    for entry_id in entry_ids:
        row = store.get_entry(entry_id)
        if row is not None and row["arxiv_id"] and not row["abstract"]:
            pending[row["arxiv_id"]] = entry_id
    if not pending:
        return 0

    updated = 0
    for arxiv_id, item in fetch_metadata(list(pending), fetch).items():
        entry_id = pending.get(arxiv_id)
        if entry_id is None or not item.title:
            continue
        links = {"abstract": item.url or f"https://arxiv.org/abs/{arxiv_id}"}
        if item.pdf_url:
            links["pdf"] = item.pdf_url
        store.update_paper_metadata(
            entry_id,
            title=item.title,
            title_norm=normalize_title(item.title),
            authors=item.authors,
            abstract=item.abstract,
            links=links,
        )
        updated += 1
    return updated


# -- scoring / selection ---------------------------------------------------
def _entry_sources(store, entry_id: int) -> set[str]:
    return {m["source"] for m in store.get_mentions(entry_id)}


def _primary_source(store, entry_id: int) -> str:
    mentions = store.get_mentions(entry_id)
    return mentions[0]["source"] if mentions else "unknown"


def _passes_gate(row, sources: set[str], trusted: bool) -> bool:
    """Trusted items bypass the gate; others need LLM relevance >= 2.

    arXiv author-feed items are a trusted whitelist (bypass), as is any mention
    flagged trusted at ingest (a trusted Slack channel, or a Slack link to a
    known paper domain). Entries not yet re-enriched under v2 fall back to the
    old boolean safety_relevant flag.
    """
    if trusted or "arxiv" in sources:
        return True
    if row["relevance"] is not None:
        return row["relevance"] >= 2
    return bool(row["safety_relevant"])


def select_digest(
    store,
    weights: ScoringWeights,
    *,
    top_n,
    candidate_start,
    resurface_start,
    source_priors: dict[str, float] | None = None,
    tracked_authors: set[str] | None = None,
) -> list[dict]:
    source_priors = source_priors or {}
    tracked_authors = tracked_authors or set()
    fb_weights = store.get_feedback_weights()
    chosen: list[dict] = []

    for entry_id in store.active_entry_ids_since(min(candidate_start, resurface_start)):
        row = store.get_entry(entry_id)
        sources = _entry_sources(store, entry_id)
        trusted = store.entry_has_trusted_mention(entry_id)
        if not _passes_gate(row, sources, trusted):
            continue

        metrics = store.latest_metrics(entry_id)
        citation_count = metrics["citation_count"] if metrics else None
        citation_prev = metrics["citation_count_prev"] if metrics else None
        new_mentions = store.count_mentions_since(entry_id, candidate_start)

        authors = json.loads(row["authors_json"])
        tags = json.loads(row["tags_json"])
        keys = derive_feedback_keys(authors, tags, _primary_source(store, entry_id))

        growth = citation_growth(citation_count, citation_prev)
        surge = new_mentions >= 2 or growth > 0
        if store.was_shown(entry_id):
            # Already seen: only reappear if still within the resurface window
            # AND freshly surging (surge measured over the candidate window).
            in_resurface = store.count_mentions_since(entry_id, resurface_start) > 0
            resurfaced = in_resurface and surge
            if not resurfaced:
                continue
        else:
            # Never shown: must be fresh (mentioned within the candidate window).
            if new_mentions == 0:
                continue
            resurfaced = False

        features = ScoreFeatures(
            distinct_sources=len(sources),
            citation_count=citation_count,
            citation_count_prev=citation_prev,
            new_mentions_in_window=new_mentions,
            feedback_affinity=feedback_affinity(keys, fb_weights),
            resurfaced=resurfaced,
            relevance=row["relevance"],
            source_prior=best_source_prior(sources, source_priors),
            tracked_author=has_tracked_author(authors, tracked_authors),
        )
        chosen.append(
            {
                "entry_id": entry_id,
                "row": row,
                "score": compute_score(features, weights),
                "features": features,
                "resurfaced": resurfaced,
                "tags": tags,
                "authors": authors,
            }
        )

    chosen.sort(key=lambda c: c["score"], reverse=True)
    return chosen[:top_n]


def _to_item(c: dict) -> DigestItem:
    row = c["row"]
    return DigestItem(
        title=row["title"],
        authors=c["authors"],
        tldr=row["tldr"],
        why=row["why"],
        tags=c["tags"],
        links=json.loads(row["links_json"]),
        score=c["score"],
        explanation=score_explanation(c["features"]),
        resurfaced=c["resurfaced"],
    )


# -- top-level pipeline ----------------------------------------------------
def run_pipeline(
    store,
    *,
    sources,
    enricher,
    sender,
    weights: ScoringWeights,
    top_n: int,
    since: str | None,
    candidate_window_days: int,
    resurface_window_days: int,
    now: datetime,
    max_enrich: int,
    dry_run: bool,
    out_dir: Path,
    metadata_fetch=None,
    source_priors: dict[str, float] | None = None,
    tracked_authors: set[str] | None = None,
) -> RunResult:
    now_iso = now.strftime(_ISO)
    candidate_start = (now - timedelta(days=candidate_window_days)).strftime(_ISO)
    resurface_start = (now - timedelta(days=resurface_window_days)).strftime(_ISO)

    new_ids = ingest(store, sources, since, now_iso)
    # Fill in real paper metadata BEFORE enrichment so the LLM judges the
    # paper's abstract, not a tweet fragment. None (tests) skips the fetch.
    if metadata_fetch is not None and new_ids:
        resolve_paper_metadata(store, new_ids, metadata_fetch)
    enriched = enrich_unenriched(store, enricher, max_enrich) if enricher else 0

    chosen = select_digest(
        store,
        weights,
        top_n=top_n,
        candidate_start=candidate_start,
        resurface_start=resurface_start,
        source_priors=source_priors,
        tracked_authors=tracked_authors,
    )
    items = [_to_item(c) for c in chosen]
    html = render_html(items, generated_at=now_iso)

    result = RunResult(
        chosen_ids=[c["entry_id"] for c in chosen],
        new_count=len(new_ids),
        enriched_count=enriched,
    )

    if dry_run:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"digest-{now.strftime('%Y%m%dT%H%M%SZ')}.html"
        path.write_text(html)
        result.digest_path = path
        return result

    if items:
        sender.send(subject=f"paper-watch digest — {len(items)} paper(s)", html=html)
        result.sent = True
    for rank, c in enumerate(chosen, start=1):
        store.record_shown(
            entry_id=c["entry_id"],
            digest_at=now_iso,
            rank=rank,
            score=c["score"],
            resurfaced=c["resurfaced"],
        )
    return result


# -- real entrypoint (wired by the CLI) ------------------------------------
def build_sources(config: Config, fetch=None, *, nitter_instances: list[str] | None = None):
    from paper_watch.http import get_text
    from paper_watch.sources.arxiv import ArxivSource
    from paper_watch.sources.rss import RssSource
    from paper_watch.sources.twitter_nitter import NitterSource

    fetch = fetch or get_text
    instances = config.nitter_instances if nitter_instances is None else nitter_instances
    sources = []
    if config.authors:
        sources.append(ArxivSource(config.authors, fetch=fetch))
    if config.feeds:
        sources.append(RssSource(config.feeds, fetch=fetch))
    if config.handles:
        sources.append(NitterSource(config.handles, instances, fetch=fetch))
    if config.slack and config.slack.workspaces:
        from paper_watch.sources.slack import SlackSource

        sources.append(
            SlackSource(config.slack.workspaces, config.slack.paper_link_domains)
        )
    return sources


class _PassthroughEnricher:
    """Used when no ANTHROPIC_API_KEY is set: marks entries enriched without an
    LLM call (relevance=2 so nothing is silently gated out; no TL;DR/tags)."""

    def enrich(self, *, title, abstract, source, mentions) -> EnrichmentResult:
        return EnrichmentResult(tldr="", why="", tags=[], relevance=2)


def _build_enricher(config: Config):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _PassthroughEnricher()
    from paper_watch.enrich import ClaudeEnricher, load_profile, load_tag_vocabulary

    return ClaudeEnricher(
        config.llm.model,
        profile=load_profile(config.llm.profile_path),
        vocabulary=load_tag_vocabulary(config.llm.tags_path),
    )


def update_metrics(store, entry_ids: list[int], now_iso: str) -> None:
    """Best-effort Semantic Scholar citation counts for entries with an arXiv id."""
    from paper_watch.sources.semantic_scholar import SemanticScholar

    s2 = SemanticScholar()
    for entry_id in entry_ids:
        row = store.get_entry(entry_id)
        if row is None or not row["arxiv_id"]:
            continue
        count = s2.citation_count(row["arxiv_id"])
        if count is not None:
            store.record_metrics(entry_id, count, now_iso)


def run(config_path: str, *, dry_run: bool = False, since: str | None = None) -> RunResult:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config = Config.load(config_path)
    from paper_watch.delivery.email import GmailSender
    from paper_watch.store import Store

    store = Store(config.db_path)
    try:
        now = datetime.now(timezone.utc)
        since_iso = since_to_iso(since or config.lookback, now=now)
        nitter_instances = config.nitter_instances
        if config.handles:
            from paper_watch.nitter_local import ensure_local_nitter

            nitter_instances = ensure_local_nitter(
                config.nitter_instances, dry_run=dry_run
            )
        sources = build_sources(config, nitter_instances=nitter_instances)
        enricher = _build_enricher(config)
        sender = GmailSender(config.smtp, os.environ.get("SMTP_APP_PASSWORD", ""))

        if not dry_run:
            pool_days = max(config.candidate_window_days, config.resurface_window_days)
            window_start = (now - timedelta(days=pool_days)).strftime(_ISO)
            update_metrics(store, store.active_entry_ids_since(window_start), now.strftime(_ISO))

        from paper_watch.http import get_text

        return run_pipeline(
            store,
            sources=sources,
            enricher=enricher,
            sender=sender,
            metadata_fetch=get_text,
            source_priors=config.source_priors,
            tracked_authors=normalize_tracked_authors(config.authors),
            weights=config.scoring,
            top_n=config.top_n,
            since=since_iso,
            candidate_window_days=config.candidate_window_days,
            resurface_window_days=config.resurface_window_days,
            now=now,
            max_enrich=config.llm.max_enrich_per_run,
            dry_run=dry_run,
            out_dir=Path("out"),
        )
    finally:
        store.close()
