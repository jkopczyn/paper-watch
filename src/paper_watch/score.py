"""Ranking. Deterministic arithmetic over cached features — zero LLM tokens
per run.

score = w_relevance·(relevance/10) + w_source·source_prior + w_feedback·affinity
      + w_author·tracked_author + w_overlap·overlap + w_velocity·velocity
      (+ resurface_boost if the paper is resurfacing)

The only LLM-derived input is `relevance`, judged once per entry at enrichment
time against the reader profile (abstract + metadata only) and cached. It is
the discriminator between fresh papers, which otherwise all look identical to
the structural signals. Every weight is tunable offline against ground truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from paper_watch.config import ScoringWeights

_VELOCITY_K = 5.0  # saturation constant: velocity_raw == K maps to 0.5
_RELEVANCE_MAX = 10  # rubric top (see enrich.RELEVANCE_RUBRIC)


@dataclass
class ScoreFeatures:
    distinct_sources: int
    citation_count: int | None
    citation_count_prev: int | None
    new_mentions_in_window: int
    feedback_affinity: float
    resurfaced: bool
    relevance: int | None = None  # 0-10, None until enriched under v2
    source_prior: float = 0.0  # best per-source base weight among mentions
    tracked_author: bool = False  # any author on the config whitelist


def citation_growth(citation_count: int | None, citation_count_prev: int | None) -> int:
    """Cited-more-since-last-measurement, or 0 without two measurements."""
    if citation_count is None or citation_count_prev is None:
        return 0
    return max(0, citation_count - citation_count_prev)


def overlap_norm(distinct_sources: int, cap: int = 3) -> float:
    """Cross-source overlap, normalized to [0, 1] and capped at `cap` sources."""
    return min(distinct_sources, cap) / cap


def velocity_norm(
    citation_count: int | None,
    citation_count_prev: int | None,
    new_mentions: int,
    k: float = _VELOCITY_K,
) -> float:
    """Citation growth + in-window mention rate, saturated into [0, 1).

    Citation growth captures older papers; the mention rate captures brand-new
    papers that have no citations yet. Growth needs two measurements — the first
    observation of an already-cited paper is not a surge.
    """
    growth = citation_growth(citation_count, citation_count_prev)
    raw = growth + new_mentions
    if raw <= 0:
        return 0.0
    return raw / (raw + k)


def relevance_norm(relevance: int | None) -> float:
    """LLM relevance 0-10 mapped to [0, 1]; unenriched entries contribute 0."""
    if relevance is None:
        return 0.0
    return max(0, min(_RELEVANCE_MAX, relevance)) / _RELEVANCE_MAX


def source_prior(source: str, priors: dict[str, float]) -> float:
    """Base weight for one source label via longest-prefix match.

    Labels are hierarchical ("slack:alignment:papers-running-list"), so
    "slack:alignment:papers-running-list" beats "slack"; the "default" key
    (or 0.5) applies when nothing matches.
    """
    best_key = None
    for key in priors:
        if key != "default" and source.startswith(key):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    if best_key is not None:
        return priors[best_key]
    return priors.get("default", 0.5)


def best_source_prior(sources: set[str], priors: dict[str, float]) -> float:
    """The strongest endorsement wins: max prior over the entry's sources."""
    if not sources:
        return priors.get("default", 0.5)
    return max(source_prior(s, priors) for s in sources)


def derive_feedback_keys(
    authors: list[str], tags: list[str], source: str
) -> list[tuple[str, str]]:
    """The keys a paper contributes to / draws affinity from."""
    keys: list[tuple[str, str]] = [("author", a) for a in authors]
    keys += [("tag", t) for t in tags]
    keys.append(("source", source))
    return keys


def feedback_affinity(
    keys: list[tuple[str, str]], weights: dict[tuple[str, str], float]
) -> float:
    """Sum learned weights for the paper's keys, squashed to (-1, 1) via tanh."""
    total = sum(weights.get(key, 0.0) for key in keys)
    return math.tanh(total)


def dynamic_feedback_weight(
    weeks: int, *, start: float = 2.0, ceiling: float = 4.0, half_life: float = 10.0
) -> float:
    """Feedback weight as a function of weeks of feedback gathered.

    Ramps from `start` toward `ceiling` with the given half-life in weeks:
    0 weeks -> 2.0, 10 -> 3.0, ~100 -> ~4.0. More accumulated group feedback
    means more trust in the learned per-author / per-tag / per-source weights.
    """
    return ceiling - (ceiling - start) * 0.5 ** (weeks / half_life)


def has_tracked_author(authors: list[str], tracked: set[str]) -> bool:
    """Case-insensitive membership of any paper author in the config whitelist."""
    return any(a.casefold().strip() in tracked for a in authors)


def normalize_tracked_authors(authors: list[str]) -> set[str]:
    return {a.casefold().strip() for a in authors}


def compute_score(f: ScoreFeatures, w: ScoringWeights) -> float:
    score = (
        w.relevance * relevance_norm(f.relevance)
        + w.source * f.source_prior
        + w.overlap * overlap_norm(f.distinct_sources)
        + w.velocity
        * velocity_norm(f.citation_count, f.citation_count_prev, f.new_mentions_in_window)
        + w.feedback * f.feedback_affinity
        + w.author * (1.0 if f.tracked_author else 0.0)
    )
    if f.resurfaced:
        score += w.resurface_boost
    return score
