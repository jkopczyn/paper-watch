"""Vote-math for the real-votes feedback import: the turnout proxy, the
votes->target curve (checked against the user's sketch), and score scaling."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from paper_watch.config import ScoringWeights
from paper_watch.feedback import (
    _score_scale,
    import_file,
    import_votes,
    poll_attendance,
    votes_to_target,
)
from paper_watch.store import Store

TS1 = str(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())  # 2026-W27
TS2 = str(datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc).timestamp())  # 2026-W28
SEEN = "2026-06-28T00:00:00Z"


def _cfg(authors=()):
    return SimpleNamespace(
        scoring=ScoringWeights(),
        source_priors={"default": 0.5, "arxiv": 0.6, "slack": 0.9},
        authors=list(authors),
        candidate_window_days=7,
    )


def _seed(store, title, *, arxiv_id, authors, relevance=8):
    eid = store.insert_entry(
        title=title, title_norm=title.lower(), first_seen_at=SEEN,
        arxiv_id=arxiv_id, authors=authors, abstract="a",
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=["interp"], relevance=relevance, version=2)
    store.add_mention(
        entry_id=eid, source="slack:far:papers",
        source_item_url=f"https://arxiv.org/abs/{arxiv_id}", fetched_at=SEEN,
    )
    return eid


def _votes_csv(path):
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{TS1},1,one,5,https://arxiv.org/abs/2605.01642,Winner\n"
        f"2026-W27,{TS1},2,two,1,https://arxiv.org/abs/2605.00002,Meh\n"
        f"2026-W27,{TS1},3,three,2,https://unknown.example/x,Unresolvable\n"
        f"2026-W27,{TS1},4,four,0,https://arxiv.org/abs/2605.00003,ZeroVote\n"
        f"2026-W28,{TS2},1,one,4,https://arxiv.org/abs/2605.01642,Winner again\n"
        f"2026-W28,{TS2},2,two,3,https://arxiv.org/abs/2605.00002,Meh again\n"
    )
    return path


def _seed_all(store):
    a = _seed(store, "Winner", arxiv_id="2605.01642", authors=["Alice"])
    b = _seed(store, "Meh", arxiv_id="2605.00002", authors=["Bob"])
    _seed(store, "ZeroVote", arxiv_id="2605.00003", authors=["Cara"])
    return a, b


def test_import_votes_counts_and_weight_directions(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    res = import_votes(store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg())

    # 2 entries x 2 weeks; the 0-vote option skipped; the unknown URL unresolved.
    assert (res.imported, res.skipped_zero, res.unresolved) == (4, 1, 1)

    weights = store.get_feedback_weights()
    # Alice swept a high-turnout poll -> positive; Bob's lone/low votes -> negative.
    assert weights[("author", "Alice")] > 0
    assert weights[("author", "Bob")] < 0
    store.close()


def test_import_votes_records_per_row_week_and_winner_picked(tmp_path):
    store = Store(tmp_path / "pw.db")
    a, b = _seed_all(store)
    import_votes(store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg())

    fb = {(r["entry_id"], r["week"]): r for r in store.conn.execute(
        "SELECT entry_id, week, picked FROM feedback"
    ).fetchall()}
    # keyed by each row's own week, not today's
    assert (a, "2026-W27") in fb and (a, "2026-W28") in fb
    # winner of the W27 poll (5 votes) is picked; the 1-vote runner-up is not
    assert fb[(a, "2026-W27")]["picked"] == 1
    assert fb[(b, "2026-W27")]["picked"] == 0
    store.close()


def test_import_votes_week_filter(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    res = import_votes(
        store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg(), week_filter="2026-W28"
    )
    # only the two W28 rows; the unresolved/zero rows live in W27
    assert (res.imported, res.skipped_zero, res.unresolved) == (2, 0, 0)
    store.close()


def test_import_file_routes_by_header(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    summary = import_file(
        store, path=_votes_csv(tmp_path / "gt.csv"), week=None, config=_cfg()
    )
    assert "vote row(s)" in summary

    cand = tmp_path / "c.csv"
    cand.write_text("entry_id,title,picked,group_rating,notes\n")  # header only
    assert "feedback row(s)" in import_file(store, path=cand, week="2026-W25", config=_cfg())

    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n")
    with pytest.raises(ValueError):
        import_file(store, path=bad, week=None, config=_cfg())
    store.close()


def test_poll_attendance_proxy_top_plus_runner_third():
    assert poll_attendance([3, 1, 0]) == pytest.approx(3 + 1 / 3)
    assert poll_attendance([7, 5, 2]) == pytest.approx(7 + 5 / 3)
    assert poll_attendance([2]) == pytest.approx(2.0)
    assert poll_attendance([]) == 0.0


def test_votes_to_target_zero_is_none():
    assert votes_to_target(0, 5) is None
    assert votes_to_target(-1, 5) is None


def test_votes_to_target_matches_brisk_week_sketch():
    # attendance 7: v=1 floored to -1, then ~+0.25/vote crossing 0 at v=3.
    a = 7
    assert votes_to_target(1, a) == pytest.approx(-1.0, abs=0.03)
    assert votes_to_target(2, a) == pytest.approx(-0.25, abs=0.03)
    assert votes_to_target(3, a) == pytest.approx(0.0, abs=0.03)
    assert votes_to_target(4, a) == pytest.approx(0.25, abs=0.03)
    assert votes_to_target(5, a) == pytest.approx(0.50, abs=0.03)
    assert votes_to_target(7, a) == pytest.approx(1.0, abs=0.03)


def test_votes_to_target_matches_slow_week_sketch():
    # attendance 3: a full sweep tops out below +1 (a smaller poll is weaker).
    a = 3
    assert votes_to_target(1, a) == pytest.approx(-0.5, abs=0.03)
    assert votes_to_target(2, a) == pytest.approx(0.15, abs=0.05)
    assert votes_to_target(3, a) == pytest.approx(0.75, abs=0.05)


def test_votes_to_target_monotonic_in_votes():
    a = 7
    vals = [votes_to_target(v, a) for v in range(2, 8)]  # skip the lone-vote floor
    assert vals == sorted(vals)


def test_score_scale_is_prediction_error_bounded():
    # neutral score 5 -> unchanged
    assert _score_scale(1.0, 5.0) == pytest.approx(1.0)
    assert _score_scale(-1.0, 5.0) == pytest.approx(-1.0)
    # high score: small boost, large penalty
    assert _score_scale(1.0, 10.0) == pytest.approx(0.0)
    assert _score_scale(-1.0, 10.0) == pytest.approx(-2.0)
    # low score: large boost, small penalty
    assert _score_scale(1.0, 0.0) == pytest.approx(2.0)
    assert _score_scale(-1.0, 0.0) == pytest.approx(0.0)
    # scores outside [0,10] are clamped
    assert _score_scale(1.0, 12.0) == pytest.approx(0.0)
    assert _score_scale(1.0, -3.0) == pytest.approx(2.0)
