"""Offline eval: score the ranker against reading-group poll ground truth.

For each poll week, replay the candidate pool the digest would have drawn from
(entries mentioned in the `window_days` before the poll), rank it with a given
weight set, and compare the top-N against what the humans voted for:

- recall@N  — fraction of voted papers (votes > 0) that made the top-N
- winner rank — where each week's top-voted paper landed (None = missed)
- nDCG@N    — vote counts as gains, so near-misses in ordering still score

Ground-truth papers that never entered the DB at all are reported separately
as ingest misses: that's a source-coverage problem, not a ranking problem.

Replay caveats (documented, deliberate): the pool is rebuilt from today's DB
(mention timestamps are historical, enrichment is current), every candidate is
treated as fresh (no shown/resurface state), and the gate is applied as today.
Weight sets are compared on identical inputs, which is what matters.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_watch.config import ScoringWeights
from paper_watch.identity import canonicalize_url, extract_arxiv_id
from paper_watch.score import (
    ScoreFeatures,
    best_source_prior,
    compute_score,
    derive_feedback_keys,
    dynamic_feedback_weight,
    feedback_affinity,
    has_tracked_author,
)
from paper_watch.store import Store

_ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class GroundTruthRow:
    week: str
    message_ts: str
    option: int
    votes: int
    url: str
    context: str
    entry_id: int | None = None  # matched lazily against the store


@dataclass
class WeekResult:
    week: str
    n_groundtruth: int
    n_matched: int
    voted_in_pool: int
    voted_in_top: int
    winner_rank: int | None
    ndcg: float
    pool_size: int


@dataclass
class EvalReport:
    weeks: list[WeekResult] = field(default_factory=list)
    ingest_misses: list[GroundTruthRow] = field(default_factory=list)

    @property
    def recall_at_n(self) -> float:
        voted = sum(w.voted_in_pool for w in self.weeks)
        hit = sum(w.voted_in_top for w in self.weeks)
        return hit / voted if voted else 0.0

    @property
    def mean_ndcg(self) -> float:
        if not self.weeks:
            return 0.0
        return sum(w.ndcg for w in self.weeks) / len(self.weeks)


def load_groundtruth(path: str | Path) -> list[GroundTruthRow]:
    rows: list[GroundTruthRow] = []
    with Path(path).open(newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                GroundTruthRow(
                    week=r["week"],
                    message_ts=r["message_ts"],
                    option=int(r["option"]),
                    votes=int(r["votes"] or 0),
                    url=r["url"],
                    context=r.get("context", ""),
                )
            )
    return rows


def match_entry(store: Store, row: GroundTruthRow, resolver=None) -> int | None:
    """Find the DB entry a ground-truth option refers to, or None (ingest miss).

    With a tweet `resolver`, a bare tweet-link option is resolved (text + links)
    and re-checked for an arXiv id, mirroring what ingest would have recovered —
    so a poll option that is a tweet doesn't count as an ingest miss just because
    its paper id lived in the tweet body.
    """
    arxiv_id = extract_arxiv_id(f"{row.url} {row.context}")
    if arxiv_id:
        entry = store.get_entry_by_arxiv_id(arxiv_id)
        if entry is not None:
            return int(entry["id"])
    hit = store.get_entry_id_by_mention_url(canonicalize_url(row.url))
    if hit is not None:
        return hit
    if resolver is not None:
        from paper_watch.sources.tweet_resolver import is_tweet_url

        canonical = canonicalize_url(row.url)
        if is_tweet_url(canonical):
            res = resolver.resolve(canonical)
            if res is not None:
                rid = extract_arxiv_id(f"{res.text or ''} {' '.join(res.links)}")
                if rid:
                    entry = store.get_entry_by_arxiv_id(rid)
                    if entry is not None:
                        return int(entry["id"])
    return None


def _poll_iso(message_ts: str) -> str:
    dt = datetime.fromtimestamp(float(message_ts), tz=timezone.utc)
    return dt.strftime(_ISO)


def _passes_gate(row, sources: set[str], trusted: bool) -> bool:
    # Mirrors runtime._passes_gate (kept in sync; runtime owns the semantics).
    if trusted or "arxiv" in sources:
        return True
    if row["relevance"] is not None:
        return row["relevance"] >= 4
    return bool(row["safety_relevant"])


def rank_pool(
    store: Store,
    *,
    start: str,
    end: str,
    weights: ScoringWeights,
    source_priors: dict[str, float],
    tracked_authors: set[str],
) -> list[int]:
    """Entry ids mentioned in [start, end] that pass the gate, best score first."""
    fb_weights = store.get_feedback_weights()
    weights = weights.model_copy(
        update={"feedback": dynamic_feedback_weight(store.count_feedback_weeks())}
    )
    scored: list[tuple[float, int]] = []
    for entry_id in store.entry_ids_mentioned_between(start, end):
        row = store.get_entry(entry_id)
        sources = {m["source"] for m in store.get_mentions(entry_id)}
        trusted = store.entry_has_trusted_mention(entry_id)
        if not _passes_gate(row, sources, trusted):
            continue
        metrics = store.latest_metrics(entry_id)
        authors = json.loads(row["authors_json"])
        tags = json.loads(row["tags_json"])
        primary = next(iter(sources)) if sources else "unknown"
        features = ScoreFeatures(
            distinct_sources=len(sources),
            citation_count=metrics["citation_count"] if metrics else None,
            citation_count_prev=metrics["citation_count_prev"] if metrics else None,
            new_mentions_in_window=store.count_mentions_between(entry_id, start, end),
            feedback_affinity=feedback_affinity(
                derive_feedback_keys(authors, tags, primary), fb_weights
            ),
            resurfaced=False,
            relevance=row["relevance"],
            source_prior=best_source_prior(sources, source_priors),
            tracked_author=has_tracked_author(authors, tracked_authors),
        )
        scored.append((compute_score(features, weights), entry_id))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [entry_id for _, entry_id in scored]


def _ndcg(ranked: list[int], gains: dict[int, int], top_n: int) -> float:
    dcg = sum(
        gains.get(eid, 0) / math.log2(i + 2)
        for i, eid in enumerate(ranked[:top_n])
    )
    ideal = sorted(gains.values(), reverse=True)[:top_n]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def evaluate(
    store: Store,
    groundtruth: list[GroundTruthRow],
    *,
    weights: ScoringWeights,
    source_priors: dict[str, float],
    tracked_authors: set[str],
    top_n: int = 15,
    window_days: int = 7,
    resolver=None,
) -> EvalReport:
    report = EvalReport()
    for row in groundtruth:
        row.entry_id = match_entry(store, row, resolver)
        if row.entry_id is None:
            report.ingest_misses.append(row)

    by_poll: dict[str, list[GroundTruthRow]] = {}
    for row in groundtruth:
        by_poll.setdefault(row.message_ts, []).append(row)

    for message_ts in sorted(by_poll):
        rows = by_poll[message_ts]
        end = _poll_iso(message_ts)
        start = (
            datetime.strptime(end, _ISO).replace(tzinfo=timezone.utc)
            - timedelta(days=window_days)
        ).strftime(_ISO)
        ranked = rank_pool(
            store,
            start=start,
            end=end,
            weights=weights,
            source_priors=source_priors,
            tracked_authors=tracked_authors,
        )
        pool = set(ranked)
        top = ranked[:top_n]

        matched = [r for r in rows if r.entry_id is not None]
        gains = {r.entry_id: r.votes for r in matched}
        voted_in_pool = [r for r in matched if r.votes > 0 and r.entry_id in pool]
        voted_in_top = [r for r in voted_in_pool if r.entry_id in top]

        winner_rank: int | None = None
        voted_rows = [r for r in matched if r.votes > 0]
        if voted_rows:
            winner = max(voted_rows, key=lambda r: r.votes)
            if winner.entry_id in pool:
                winner_rank = ranked.index(winner.entry_id) + 1

        report.weeks.append(
            WeekResult(
                week=rows[0].week,
                n_groundtruth=len(rows),
                n_matched=len(matched),
                voted_in_pool=len(voted_in_pool),
                voted_in_top=len(voted_in_top),
                winner_rank=winner_rank,
                ndcg=_ndcg(ranked, gains, top_n),
                pool_size=len(ranked),
            )
        )
    return report
