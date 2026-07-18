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


def effective_since(store, since: str | None, lookback: str, now: datetime) -> str:
    """Fetch cutoff for this run, widened to cover any gap since the last run.

    Normally this is the configured `lookback` window (e.g. 7d). But if the
    machine was powered off across one or more scheduled runs, the last recorded
    run can be further in the past than `lookback` — in that case we fetch from
    the last run so the gap is fully covered and nothing is missed. An explicit
    `--since` always wins and is passed through unchanged.
    """
    since_iso = since_to_iso(since or lookback, now=now)
    if since is None:
        last_run = store.get_last_run_at()
        # ISO-8601 'Z' strings compare lexicographically == chronologically.
        if last_run and last_run < since_iso:
            since_iso = last_run
    return since_iso


# -- ingest ----------------------------------------------------------------
def _ingest_one(store, raw, now_iso: str, tweet_resolver, new_ids: list[int]) -> None:
    canonical = canonicalize_url(raw.url)
    if canonical != raw.url:
        raw = replace(raw, url=canonical)
    if tweet_resolver is not None:
        raw = tweet_resolver.augment(raw)
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


def ingest(
    store,
    sources,
    since: str | None,
    now_iso: str,
    *,
    tweet_resolver=None,
    newsletter_extractor=None,
) -> list[int]:
    """Fetch every source, normalize, dedup into entries, and record mentions.

    `tweet_resolver` (when set) recovers the paper a bare tweet link points at.
    `newsletter_extractor` (when set) fans a newsletter item out into the papers
    it links, each ingested as its own entry with the newsletter as provenance —
    the newsletter itself still doesn't adopt any linked paper's identity.
    Returns the ids of entries newly created this run.
    """
    new_ids: list[int] = []
    for source in sources:
        for raw in source.fetch(since):
            _ingest_one(store, raw, now_iso, tweet_resolver, new_ids)
            if newsletter_extractor is not None and raw.source.startswith("rss"):
                for extra in newsletter_extractor(raw):
                    _ingest_one(store, extra, now_iso, tweet_resolver, new_ids)
    return new_ids


def _entry_pdf_url(row) -> str | None:
    """The PDF link (or a `.pdf` abstract URL) of an entry, if any."""
    links = json.loads(row["links_json"])
    pdf = links.get("pdf")
    if pdf:
        return pdf
    abstract_url = links.get("abstract") or ""
    return abstract_url if abstract_url.lower().endswith(".pdf") else None


def rewrite_paper_metadata(
    store,
    entry_id: int,
    *,
    title: str,
    authors: list[str],
    abstract: str | None,
    links: dict[str, str],
    published_at: str | None = None,
) -> int:
    """Land resolved metadata on an entry, merging it away if it now has a twin.

    Resolution is the moment two entries can be revealed as the same paper: the
    AF post and the arXiv link it cites are both born titled with their own URL
    and only collide once the real title arrives. Merge rather than leave a twin.
    The older id wins, so existing references stay valid. Returns the survivor.
    """
    from paper_watch.identity import is_distinctive_title, normalize_title

    title_norm = normalize_title(title)
    store.update_paper_metadata(
        entry_id,
        title=title,
        title_norm=title_norm,
        authors=authors,
        abstract=abstract,
        links=links,
        published_at=published_at,
    )
    row = store.get_entry(entry_id)
    if row is None:
        return entry_id
    twin = store.find_twin_entry_id(
        entry_id,
        arxiv_id=row["arxiv_id"],
        doi=row["doi"],
        # Only a distinctive title is identity: two unrelated PDFs can both
        # resolve to "System Card", and merging those loses a paper outright.
        title_norm=title_norm if is_distinctive_title(title_norm) else None,
    )
    if twin is None:
        return entry_id
    winner, loser = min(entry_id, twin), max(entry_id, twin)
    store.merge_entries(winner_id=winner, loser_id=loser)
    return winner


def _is_html_page_url(url: str) -> bool:
    """An http(s) page to scrape for metadata — not a PDF (that's the PDF path)."""
    return url.startswith(("http://", "https://")) and not url.lower().endswith(".pdf")


def _has_http_link(row) -> bool:
    return any(
        isinstance(v, str) and v.startswith("http")
        for v in json.loads(row["links_json"]).values()
    )


def resolve_paper_metadata(
    store,
    entry_ids: list[int],
    fetch,
    *,
    openreview_resolver=None,
    pdf_resolver=None,
    html_resolver=None,
    search_resolver=None,
    reresolve=False,
) -> int:
    """Give post-shaped entries their real paper metadata, best-effort.

    A tweet/Slack/newsletter entry that links a paper is created with the post
    text (or bare URL) as its title and no abstract; the LLM gate and any
    content-based ranking need the actual paper. Resolves entries with no
    abstract by landing-page type: arXiv id → batched arXiv API (needs `fetch`);
    an OpenReview forum link → `openreview_resolver`; a raw PDF link →
    `pdf_resolver`; any other HTML page → `html_resolver` (its Open Graph / title
    metadata). Each is best-effort; one failure never aborts the rest. Returns
    how many entries were updated.

    `reresolve` reprocesses entries that already have an abstract — off in the
    live pipeline (an abstract means already resolved), on for the backfill that
    re-runs a curated set of junk-titled entries through the fixed resolvers.
    """
    from paper_watch.sources.arxiv import fetch_metadata
    from paper_watch.sources.openreview import forum_id

    arxiv_pending: dict[str, int] = {}
    openreview_pending: list[tuple[int, str]] = []
    pdf_pending: list[tuple[int, str]] = []
    html_pending: list[tuple[int, str]] = []
    search_pending: list[tuple[int, str]] = []
    for entry_id in entry_ids:
        row = store.get_entry(entry_id)
        if row is None or (row["abstract"] and not reresolve):
            continue
        if row["arxiv_id"]:
            arxiv_pending[row["arxiv_id"]] = entry_id
            continue
        abstract_url = json.loads(row["links_json"]).get("abstract") or ""
        if openreview_resolver is not None and forum_id(abstract_url):
            openreview_pending.append((entry_id, abstract_url))
        elif pdf_resolver is not None and (pdf := _entry_pdf_url(row)):
            pdf_pending.append((entry_id, pdf))
        elif html_resolver is not None and _is_html_page_url(abstract_url):
            html_pending.append((entry_id, abstract_url))
        elif search_resolver is not None and not _has_http_link(row):
            # No link to resolve from at all: search for the title instead.
            search_pending.append((entry_id, row["title"]))

    updated = 0

    if fetch is not None and arxiv_pending:
        for arxiv_id, item in fetch_metadata(list(arxiv_pending), fetch).items():
            entry_id = arxiv_pending.get(arxiv_id)
            if entry_id is None or not item.title:
                continue
            links = {"abstract": item.url or f"https://arxiv.org/abs/{arxiv_id}"}
            if item.pdf_url:
                links["pdf"] = item.pdf_url
            rewrite_paper_metadata(
                store,
                entry_id,
                title=item.title,
                authors=item.authors,
                abstract=item.abstract,
                links=links,
                published_at=item.published_at,
            )
            updated += 1

    for entry_id, url in openreview_pending:
        meta = _safe_resolve(openreview_resolver, url)
        if meta and meta.get("title"):
            rewrite_paper_metadata(
                store,
                entry_id,
                title=meta["title"],
                authors=meta.get("authors") or [],
                abstract=meta.get("abstract"),
                links={},
            )
            updated += 1
        else:
            # OpenReview's API sits behind a login/challenge gate we can't pass
            # (no bot account allowed), so the abstract is unreadable. Per Jacob:
            # flag these medium-high by default and keep the link's own metadata.
            _flag_openreview_fallback(store, entry_id)
            updated += 1

    for pending, resolver in ((pdf_pending, pdf_resolver), (html_pending, html_resolver)):
        for entry_id, url in pending:
            meta = _safe_resolve(resolver, url)
            if meta and meta.get("title"):
                rewrite_paper_metadata(
                    store,
                    entry_id,
                    title=meta["title"],
                    authors=meta.get("authors") or [],
                    abstract=meta.get("abstract"),
                    links={},
                )
                updated += 1

    for entry_id, title in search_pending:
        meta = _safe_search(search_resolver, title)
        if meta and meta.get("url"):
            rewrite_paper_metadata(
                store,
                entry_id,
                title=meta.get("title") or title,
                authors=meta.get("authors") or [],
                abstract=meta.get("abstract"),
                links={"abstract": meta["url"]},
                published_at=meta.get("published_at"),
            )
            updated += 1

    return updated


# Unreadable OpenReview submissions (API is login/challenge-gated) get this
# relevance prior so they surface as likely medium-high rather than being gated
# out on an empty abstract. 8 = "a strong pick" on the 0-10 scale (see enrich rubric).
_OPENREVIEW_PRIOR_RELEVANCE = 8


def _flag_openreview_fallback(store, entry_id: int) -> None:
    """Give an unresolvable OpenReview entry a medium-high prior + its link metadata.

    Promotes the link's own blurb (mention/anchor text) to the title when all we
    had was the bare URL, and pins relevance so the LLM (which would judge an
    abstract-less title low) doesn't override it — `enrich_unenriched` skips
    entries already at the current version.
    """
    from paper_watch.enrich import ENRICH_VERSION

    row = store.get_entry(entry_id)
    if row is None:
        return
    blurb = max(
        (m["mention_text"] or "" for m in store.get_mentions(entry_id)),
        key=len,
        default="",
    ).strip()
    links = json.loads(row["links_json"])
    if row["title"] == (links.get("abstract") or "") and blurb:
        # Promoting the blurb to the title can reveal a twin, and the merge that
        # follows may delete `entry_id` — enrich whichever entry survives.
        entry_id = rewrite_paper_metadata(
            store,
            entry_id,
            title=blurb[:200],
            authors=[],
            abstract=None,
            links={},
        )
    store.set_enrichment(
        entry_id,
        tldr=blurb[:280],
        why="OpenReview submission — abstract behind a login gate; flagged medium-high by default.",
        tags=[],
        relevance=_OPENREVIEW_PRIOR_RELEVANCE,
        version=ENRICH_VERSION,
    )


def _safe_resolve(resolver, url: str) -> dict | None:
    try:
        return resolver.resolve(url)
    except Exception as exc:  # best-effort: a bad landing page is never fatal
        import logging

        logging.getLogger(__name__).warning("metadata resolve failed for %s: %s", url, exc)
        return None


def _safe_search(resolver, title: str) -> dict | None:
    try:
        return resolver.search(title)
    except Exception as exc:  # best-effort: a failed search is never fatal
        import logging

        logging.getLogger(__name__).warning("title search failed for %r: %s", title, exc)
        return None


def _is_titleless(row) -> bool:
    """True for an entry that is effectively just a URL: no abstract, and a title
    that is a bare URL or too generic to be a real one."""
    from paper_watch.identity import is_distinctive_title

    if row["abstract"]:
        return False
    title = row["title"] or ""
    return title.startswith("http") or not is_distinctive_title(row["title_norm"])


def _entry_lookup_url(store, row) -> str | None:
    """A URL to search for this entry: its abstract link, else any mention URL."""
    url = json.loads(row["links_json"]).get("abstract")
    if url:
        return url
    for mention in store.get_mentions(row["id"]):
        if mention["source_item_url"]:
            return mention["source_item_url"]
    return None


def recover_titles(store, entry_ids: list[int], resolver) -> int:
    """Web-search the URL of each title-less entry to recover its real metadata.

    For an entry that is just a URL with no title/abstract, ask the web-search
    resolver for the work's title (+ snippet/abstract), then land it so the LLM
    gate judges a real paper instead of a bare link. Best-effort; returns how many
    entries were updated. Recovering a title can reveal a twin — the rewrite
    merges it, exactly as the other resolvers do.
    """
    if resolver is None:
        return 0
    updated = 0
    for entry_id in entry_ids:
        row = store.get_entry(entry_id)
        if row is None or not _is_titleless(row):
            continue
        url = _entry_lookup_url(store, row)
        if not url:
            continue
        blurb = max(
            (m["mention_text"] or "" for m in store.get_mentions(entry_id)),
            key=len,
            default="",
        ).strip() or None
        try:
            meta = resolver.resolve(url, blurb)
        except Exception as exc:  # best-effort: a failed search is never fatal
            import logging

            logging.getLogger(__name__).warning("web title recovery failed for %s: %s", url, exc)
            continue
        if meta and meta.get("title"):
            rewrite_paper_metadata(
                store,
                entry_id,
                title=meta["title"][:300],
                authors=[],
                abstract=meta.get("abstract") or meta.get("snippet"),
                links={},
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
    """Trusted items bypass the gate; others need LLM relevance >= 4.

    arXiv author-feed items are a trusted whitelist (bypass), as is any mention
    flagged trusted at ingest (a trusted Slack channel, or a Slack link to a
    known paper domain). Entries not yet re-enriched under v2 fall back to the
    old boolean safety_relevant flag.
    """
    if trusted or "arxiv" in sources:
        return True
    if row["relevance"] is not None:
        return row["relevance"] >= 4
    return bool(row["safety_relevant"])


def select_digest(
    store,
    weights: ScoringWeights,
    *,
    top_n,
    candidate_start,
    resurface_start,
    new_start: str | None = None,
    max_new: int | None = None,
    resurface_min_mentions: int = 2,
    source_priors: dict[str, float] | None = None,
    tracked_authors: set[str] | None = None,
) -> list[dict]:
    """Assemble the digest: lead with fresh papers, pad with resurfacing ones.

    A never-shown paper is "new" if it was mentioned within `new_start` (the last
    24h by default). The digest takes up to `max_new` of them by score; the
    remaining slots up to `top_n` are filled with resurfacing papers that outscore
    the new picks' average — so a stale classic only reappears when it genuinely
    beats this run's fresh crop. `new_start` defaults to `candidate_start` and
    `max_new` to unbounded, which reproduces a single ranked pool.
    """
    source_priors = source_priors or {}
    tracked_authors = tracked_authors or set()
    new_start = new_start or candidate_start
    fb_weights = store.get_feedback_weights()
    new_items: list[dict] = []
    resurfaced_items: list[dict] = []

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

        # A surge is fresh *attention*, and it is counted in occasions rather than
        # raw mentions. Not citation drift: a well-known paper's citation count
        # ticks up on nearly every measurement, which re-admitted the same classics
        # (GPT-3, Scaling Laws) every run for as long as they stayed in the window.
        # And not link count: one post linking a paper as the post, the arXiv abs
        # and the PDF is one act of attention, not three.
        occasions = store.count_mention_occasions_since(entry_id, candidate_start)
        surge = occasions >= resurface_min_mentions
        if store.was_shown(entry_id):
            # Already seen: only reappear if still within the resurface window
            # AND freshly surging (surge measured over the candidate window).
            in_resurface = store.count_mentions_since(entry_id, resurface_start) > 0
            if not (in_resurface and surge):
                continue
            resurfaced = True
        else:
            # Never shown: must be fresh (mentioned within the new window).
            if store.count_mentions_since(entry_id, new_start) == 0:
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
        (resurfaced_items if resurfaced else new_items).append(
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

    new_items.sort(key=lambda c: c["score"], reverse=True)
    selected_new = new_items if max_new is None else new_items[:max_new]

    avg_new = (
        sum(c["score"] for c in selected_new) / len(selected_new)
        if selected_new
        else 0.0
    )
    resurfaced_items.sort(key=lambda c: c["score"], reverse=True)
    padding = [c for c in resurfaced_items if c["score"] > avg_new]

    return (selected_new + padding)[:top_n]


def _pub_display(store, row) -> tuple[str, bool]:
    """(text, is_estimate) publication date for the digest.

    An authoritative `entries.published_at` shows exact; otherwise we estimate
    from the earliest mention's published_at (the real submit date for an arXiv
    mention, the post date for a tweet/blog), falling back to first_seen_at.
    Formatted as YYYY-MM.
    """
    real = row["published_at"]
    if real:
        return real[:7], False
    estimate = store.earliest_published_at(row["id"]) or row["first_seen_at"]
    return estimate[:7], True


def _display_links(store, entry_id: int, links: dict[str, str]) -> dict[str, str]:
    """The links to show; fall back to a URL the entry owns when it has none."""
    if links:
        return links
    for mention in store.get_mentions(entry_id):
        if mention["source_item_url"]:
            return {"link": mention["source_item_url"]}
    return links


def _to_item(store, c: dict, *, recent_start: str) -> DigestItem:
    row = c["row"]
    entry_id = c["entry_id"]
    pub_display, pub_is_estimate = _pub_display(store, row)
    source_types = sorted({s.split(":", 1)[0] for s in _entry_sources(store, entry_id)})
    return DigestItem(
        title=row["title"],
        authors=c["authors"],
        tldr=row["tldr"],
        why=row["why"],
        tags=c["tags"],
        links=_display_links(store, entry_id, json.loads(row["links_json"])),
        score=c["score"],
        explanation=score_explanation(c["features"]),
        resurfaced=c["resurfaced"],
        pub_display=pub_display,
        pub_is_estimate=pub_is_estimate,
        surfaced_recent=store.count_shown_since(entry_id, recent_start),
        source_types=source_types,
        trusted=store.entry_has_trusted_mention(entry_id),
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
    new_window: str = "24h",
    max_new: int = 10,
    recent_window: str = "48h",
    resurface_min_mentions: int = 2,
    now: datetime,
    max_enrich: int,
    dry_run: bool,
    out_dir: Path,
    metadata_fetch=None,
    source_priors: dict[str, float] | None = None,
    tracked_authors: set[str] | None = None,
    tweet_resolver=None,
    newsletter_extractor=None,
    openreview_resolver=None,
    pdf_resolver=None,
    html_resolver=None,
    search_resolver=None,
    web_search_resolver=None,
) -> RunResult:
    now_iso = now.strftime(_ISO)
    candidate_start = (now - timedelta(days=candidate_window_days)).strftime(_ISO)
    resurface_start = (now - timedelta(days=resurface_window_days)).strftime(_ISO)
    new_start = since_to_iso(new_window, now=now)
    recent_start = since_to_iso(recent_window, now=now)

    new_ids = ingest(
        store,
        sources,
        since,
        now_iso,
        tweet_resolver=tweet_resolver,
        newsletter_extractor=newsletter_extractor,
    )
    # Fill in real paper metadata BEFORE enrichment so the LLM judges the
    # paper's abstract, not a tweet fragment. None (tests) skips the arXiv fetch;
    # the OpenReview/PDF resolvers are independent and also default off.
    if new_ids and (
        metadata_fetch is not None
        or openreview_resolver
        or pdf_resolver
        or html_resolver
        or search_resolver
    ):
        resolve_paper_metadata(
            store,
            new_ids,
            metadata_fetch,
            openreview_resolver=openreview_resolver,
            pdf_resolver=pdf_resolver,
            html_resolver=html_resolver,
            search_resolver=search_resolver,
        )
    # Last resort for entries that are still just a URL (no title, no abstract):
    # a web search to recover the work's title/snippet/abstract.
    if new_ids and web_search_resolver is not None:
        recover_titles(store, new_ids, web_search_resolver)
    enriched = enrich_unenriched(store, enricher, max_enrich) if enricher else 0

    chosen = select_digest(
        store,
        weights,
        top_n=top_n,
        candidate_start=candidate_start,
        resurface_start=resurface_start,
        new_start=new_start,
        max_new=max_new,
        resurface_min_mentions=resurface_min_mentions,
        source_priors=source_priors,
        tracked_authors=tracked_authors,
    )
    items = [_to_item(store, c, recent_start=recent_start) for c in chosen]
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
def build_sources(
    config: Config,
    fetch=None,
    *,
    nitter_instances: list[str] | None = None,
    store=None,
):
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
    if config.graphql:
        from paper_watch.sources.graphql import GraphqlSource

        sources.append(GraphqlSource(config.graphql))
    # Watched pages diff against a seen-link set persisted in the store, so
    # they only exist when a store is wired in (the real `run` entrypoint).
    if config.pages and store is not None:
        from paper_watch.sources.page_watch import PageWatchSource

        sources.append(PageWatchSource(config.pages, store, fetch=fetch))
    if config.handles:
        sources.append(
            NitterSource(
                config.handles,
                instances,
                fetch=fetch,
                min_interval=config.nitter_min_interval,
            )
        )
    if config.slack and config.slack.workspaces:
        from paper_watch.sources.slack import SlackSource

        sources.append(
            SlackSource(config.slack.workspaces, config.slack.paper_link_domains)
        )
    return sources


class _PassthroughEnricher:
    """Used when no ANTHROPIC_API_KEY is set: marks entries enriched without an
    LLM call (relevance=5 so nothing is silently gated out; no TL;DR/tags)."""

    def enrich(self, *, title, abstract, source, mentions) -> EnrichmentResult:
        return EnrichmentResult(tldr="", why="", tags=[], relevance=5)


def _build_enricher(config: Config):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _PassthroughEnricher()
    from paper_watch.enrich import ClaudeEnricher, load_profile, load_tag_vocabulary

    return ClaudeEnricher(
        config.llm.model,
        profile=load_profile(config.llm.profile_path),
        vocabulary=load_tag_vocabulary(config.llm.tags_path),
    )


def _build_tweet_resolver(config: Config, store, nitter_instances: list[str]):
    """A TweetResolver bound to the surviving local Nitter instance, or None.

    Never falls back to a public mirror for per-status fetches — no local
    instance means no resolver.
    """
    if not config.tweet_resolution:
        return None
    from paper_watch.nitter_local import _is_local

    local = next((u for u in nitter_instances if _is_local(u)), None)
    if local is None:
        return None
    from paper_watch.sources.tweet_resolver import TweetResolver

    return TweetResolver(store, local)


def _build_newsletter_extractor(config: Config):
    if not config.newsletter_links:
        return None
    from paper_watch.config import _DEFAULT_PAPER_LINK_DOMAINS
    from paper_watch.sources.newsletter_links import extract_paper_links

    domains = (
        config.slack.paper_link_domains
        if config.slack
        else list(_DEFAULT_PAPER_LINK_DOMAINS)
    )
    return lambda raw: extract_paper_links(raw, domains)


def _build_metadata_resolvers(config: Config):
    """(openreview, pdf, html) resolvers for the metadata step; PDF OCR is only
    wired when an Anthropic key is present (born-digital PDFs never need it)."""
    from paper_watch.sources.html_meta import HtmlMetaResolver
    from paper_watch.sources.openreview import OpenReviewResolver
    from paper_watch.sources.pdf_meta import PdfMetaResolver

    ocr = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        from paper_watch.sources.pdf_meta import ClaudePdfOcr

        ocr = ClaudePdfOcr(config.llm.model)
    return OpenReviewResolver(), PdfMetaResolver(ocr=ocr), HtmlMetaResolver()


def _build_search_resolver(config: Config):
    """A title-search resolver to fill entries left with no link, or None."""
    if not config.url_search:
        return None
    from paper_watch.sources.paper_search import PaperSearchResolver

    return PaperSearchResolver()


def _build_web_search_resolver(config: Config):
    """A Claude web-search resolver to recover URL-only entries' titles, or None.

    Key-gated like the PDF-OCR fallback: without an Anthropic key there is no
    resolver, and the pipeline simply leaves the bare-URL entry as-is."""
    if not config.url_search or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from paper_watch.sources.web_search import WebSearchResolver

    return WebSearchResolver(config.llm.model)


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
        since_iso = effective_since(store, since, config.lookback, now)
        nitter_instances = config.nitter_instances
        if config.handles:
            from paper_watch.nitter_local import ensure_local_nitter

            nitter_instances = ensure_local_nitter(
                config.nitter_instances, dry_run=dry_run
            )
        sources = build_sources(config, nitter_instances=nitter_instances, store=store)
        enricher = _build_enricher(config)
        sender = GmailSender(config.smtp, os.environ.get("SMTP_APP_PASSWORD", ""))

        if not dry_run:
            pool_days = max(config.candidate_window_days, config.resurface_window_days)
            window_start = (now - timedelta(days=pool_days)).strftime(_ISO)
            update_metrics(store, store.active_entry_ids_since(window_start), now.strftime(_ISO))

        from paper_watch.http import get_text

        tweet_resolver = _build_tweet_resolver(config, store, nitter_instances)
        newsletter_extractor = _build_newsletter_extractor(config)
        openreview_resolver, pdf_resolver, html_resolver = _build_metadata_resolvers(config)
        search_resolver = _build_search_resolver(config)
        web_search_resolver = _build_web_search_resolver(config)

        result = run_pipeline(
            store,
            sources=sources,
            enricher=enricher,
            sender=sender,
            metadata_fetch=get_text,
            tweet_resolver=tweet_resolver,
            newsletter_extractor=newsletter_extractor,
            openreview_resolver=openreview_resolver,
            pdf_resolver=pdf_resolver,
            html_resolver=html_resolver,
            search_resolver=search_resolver,
            web_search_resolver=web_search_resolver,
            source_priors=config.source_priors,
            tracked_authors=normalize_tracked_authors(config.authors),
            weights=config.scoring,
            top_n=config.top_n,
            since=since_iso,
            candidate_window_days=config.candidate_window_days,
            resurface_window_days=config.resurface_window_days,
            new_window=config.new_window,
            max_new=config.max_new,
            recent_window=config.recent_window,
            resurface_min_mentions=config.resurface_min_mentions,
            now=now,
            max_enrich=config.llm.max_enrich_per_run,
            dry_run=dry_run,
            out_dir=Path("out"),
        )
        # Record the watermark only for real runs, so the next run covers the
        # gap from here even if the machine is off across scheduled elapses. A
        # dry run must not advance it.
        if not dry_run:
            store.set_last_run_at(now.strftime(_ISO))
        return result
    finally:
        store.close()
