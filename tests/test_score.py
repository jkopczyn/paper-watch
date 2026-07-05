import math

import pytest

from paper_watch.config import ScoringWeights
from paper_watch.score import (
    ScoreFeatures,
    citation_growth,
    compute_score,
    derive_feedback_keys,
    feedback_affinity,
    overlap_norm,
    velocity_norm,
)


# -- overlap ---------------------------------------------------------------
def test_overlap_norm_scales_to_cap():
    assert overlap_norm(0) == 0.0
    assert overlap_norm(1, cap=3) == pytest.approx(1 / 3)
    assert overlap_norm(3, cap=3) == 1.0
    assert overlap_norm(5, cap=3) == 1.0  # capped


# -- velocity --------------------------------------------------------------
def test_velocity_norm_zero_when_no_activity():
    assert velocity_norm(citation_count=0, citation_count_prev=0, new_mentions=0) == 0.0
    assert velocity_norm(citation_count=None, citation_count_prev=None, new_mentions=0) == 0.0


def test_velocity_norm_monotonic_and_bounded():
    low = velocity_norm(citation_count=2, citation_count_prev=0, new_mentions=0)
    high = velocity_norm(citation_count=20, citation_count_prev=0, new_mentions=0)
    assert 0 < low < high < 1


def test_velocity_uses_mention_rate_for_new_papers():
    # brand-new paper, no citations yet, but mentioned a lot this window
    v = velocity_norm(citation_count=0, citation_count_prev=0, new_mentions=4)
    assert v > 0


def test_first_citation_observation_is_not_growth():
    # an already-cited paper measured for the first time has no baseline;
    # its whole count must not read as a surge
    assert citation_growth(79, None) == 0
    assert velocity_norm(citation_count=79, citation_count_prev=None, new_mentions=0) == 0.0
    # with a baseline, growth counts
    assert citation_growth(81, 79) == 2
    assert citation_growth(70, 79) == 0  # S2 corrections never go negative


# -- feedback affinity -----------------------------------------------------
def test_feedback_affinity_empty_is_zero():
    assert feedback_affinity([], {}) == 0.0


def test_feedback_affinity_sums_then_squashes():
    keys = [("author", "Neel Nanda"), ("tag", "interp")]
    weights = {("author", "Neel Nanda"): 1.0, ("tag", "interp"): 0.5}
    expected = math.tanh(1.5)
    assert feedback_affinity(keys, weights) == pytest.approx(expected)


def test_feedback_affinity_bounded():
    # large magnitudes saturate but never escape [-1, 1]
    assert feedback_affinity([("author", "X")], {("author", "X"): 100.0}) <= 1.0
    assert feedback_affinity([("author", "Y")], {("author", "Y"): -100.0}) >= -1.0
    # a moderate weight stays strictly inside the bound
    assert 0 < feedback_affinity([("author", "Z")], {("author", "Z"): 0.8}) < 1.0


def test_derive_feedback_keys():
    keys = derive_feedback_keys(
        authors=["Neel Nanda"], tags=["interp", "evals"], source="arxiv"
    )
    assert ("author", "Neel Nanda") in keys
    assert ("tag", "interp") in keys
    assert ("source", "arxiv") in keys


# -- combined score --------------------------------------------------------
def test_compute_score_weights_components():
    w = ScoringWeights(overlap=2.0, velocity=1.0, feedback=1.0, resurface_boost=5.0)
    f = ScoreFeatures(
        distinct_sources=3,  # overlap_norm = 1.0
        citation_count=0,
        citation_count_prev=0,
        new_mentions_in_window=0,  # velocity = 0
        feedback_affinity=0.5,
        resurfaced=False,
    )
    # 2.0*1.0 + 1.0*0 + 1.0*0.5
    assert compute_score(f, w) == pytest.approx(2.5)


def test_resurface_adds_boost():
    w = ScoringWeights(overlap=1.0, velocity=1.0, feedback=1.0, resurface_boost=5.0)
    base = ScoreFeatures(1, 0, 0, 0, 0.0, resurfaced=False)
    boosted = ScoreFeatures(1, 0, 0, 0, 0.0, resurfaced=True)
    assert compute_score(boosted, w) - compute_score(base, w) == pytest.approx(5.0)
