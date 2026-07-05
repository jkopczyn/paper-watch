from datetime import datetime, timezone

from paper_watch.config import ScoringWeights
from paper_watch.eval import GroundTruthRow, evaluate, load_groundtruth, match_entry
from paper_watch.store import Store

POLL_TS = str(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())
IN_WINDOW = "2026-06-28T00:00:00Z"
PRIORS = {"default": 0.5, "arxiv": 0.6, "slack": 0.9}


def _paper(store, title, *, arxiv_id=None, relevance, source, url):
    eid = store.insert_entry(
        title=title,
        title_norm=title.lower(),
        first_seen_at=IN_WINDOW,
        arxiv_id=arxiv_id,
        abstract="abstract",
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=["interp"], relevance=relevance, version=2)
    store.add_mention(
        entry_id=eid, source=source, source_item_url=url, fetched_at=IN_WINDOW
    )
    return eid


def _gt(option, url, votes, ts=POLL_TS):
    return GroundTruthRow(
        week="2026-W27", message_ts=ts, option=option, votes=votes, url=url, context=""
    )


def _make_store(tmp_path):
    store = Store(tmp_path / "pw.db")
    a = _paper(
        store, "Winner Paper", arxiv_id="2605.01642", relevance=4,
        source="slack:far:papers", url="https://arxiv.org/abs/2605.01642",
    )
    b = _paper(
        store, "Runner Up", relevance=2,
        source="slack:far:papers", url="https://www.lesswrong.com/posts/abc123/x",
    )
    _paper(
        store, "Gated Noise", relevance=0,
        source="rss:Some Blog", url="https://blog.example/post",
    )
    return store, a, b


def test_match_entry_by_arxiv_id_and_mention_url(tmp_path):
    store, a, b = _make_store(tmp_path)
    assert match_entry(store, _gt(1, "https://arxiv.org/abs/2605.01642", 3)) == a
    assert match_entry(store, _gt(2, "https://www.lesswrong.com/posts/abc123/x", 1)) == b
    assert match_entry(store, _gt(3, "https://unknown.example/paper", 2)) is None
    store.close()


def test_evaluate_scores_a_week(tmp_path):
    store, a, b = _make_store(tmp_path)
    groundtruth = [
        _gt(1, "https://arxiv.org/abs/2605.01642", 3),
        _gt(2, "https://www.lesswrong.com/posts/abc123/x", 1),
        _gt(3, "https://unknown.example/paper", 2),  # ingest miss
    ]
    report = evaluate(
        store,
        groundtruth,
        weights=ScoringWeights(),
        source_priors=PRIORS,
        tracked_authors=set(),
        top_n=2,
        window_days=7,
    )
    assert len(report.weeks) == 1
    week = report.weeks[0]
    assert week.pool_size == 2  # Gated Noise (relevance 0) excluded
    assert week.n_matched == 2
    assert week.voted_in_pool == 2
    assert week.voted_in_top == 2
    assert week.winner_rank == 1  # relevance 4 beats relevance 2
    assert week.ndcg > 0.9
    assert [m.url for m in report.ingest_misses] == ["https://unknown.example/paper"]
    assert report.recall_at_n == 1.0
    store.close()


def test_evaluate_window_excludes_out_of_window_mentions(tmp_path):
    store, a, b = _make_store(tmp_path)
    # a poll far in the future: nothing was mentioned in its 7-day window
    future_ts = str(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    report = evaluate(
        store,
        [_gt(1, "https://arxiv.org/abs/2605.01642", 3, ts=future_ts)],
        weights=ScoringWeights(),
        source_priors=PRIORS,
        tracked_authors=set(),
        top_n=5,
        window_days=7,
    )
    assert report.weeks[0].pool_size == 0
    assert report.weeks[0].winner_rank is None
    store.close()


def test_load_groundtruth_roundtrip(tmp_path):
    p = tmp_path / "gt.csv"
    p.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        "2026-W27,1.0,1,one,4,https://arxiv.org/abs/2605.01642,ctx\n"
        "2026-W27,1.0,2,two,,https://b.example/y,\n"
    )
    rows = load_groundtruth(p)
    assert rows[0].votes == 4 and rows[1].votes == 0
    assert rows[1].url == "https://b.example/y"
