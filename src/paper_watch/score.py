"""Ranking. Transparent, tunable, and NOT ML.

score = w_overlap·overlap + w_velocity·velocity + w_feedback·feedback_affinity
        (+ resurface_boost if the paper is resurfacing)

The LLM plays no part here by design — ranking is driven by cross-source overlap,
citation/social velocity, and learned reading-group feedback weights.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from paper_watch.config import ScoringWeights

_VELOCITY_K = 5.0  # saturation constant: velocity_raw == K maps to 0.5


@dataclass
class ScoreFeatures:
    distinct_sources: int
    citation_count: int | None
    citation_count_prev: int | None
    new_mentions_in_window: int
    feedback_affinity: float
    resurfaced: bool


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


def compute_score(f: ScoreFeatures, w: ScoringWeights) -> float:
    score = (
        w.overlap * overlap_norm(f.distinct_sources)
        + w.velocity
        * velocity_norm(f.citation_count, f.citation_count_prev, f.new_mentions_in_window)
        + w.feedback * f.feedback_affinity
    )
    if f.resurfaced:
        score += w.resurface_boost
    return score
