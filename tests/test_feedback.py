import csv
from pathlib import Path

from paper_watch.feedback import export_candidates, import_feedback
from paper_watch.store import Store


def _seed_shown(store: Store, title, authors, tags, source, digest_at="2026-06-18T08:00:00Z"):
    eid = store.insert_entry(
        title=title,
        title_norm=title.lower(),
        first_seen_at="2026-06-17T00:00:00Z",
        authors=authors,
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=tags, relevance=3, version=2)
    store.add_mention(
        entry_id=eid, source=source, source_item_url=f"u/{eid}", fetched_at=digest_at
    )
    store.record_shown(entry_id=eid, digest_at=digest_at, rank=1, score=1.0, resurfaced=False)
    return eid


def test_export_candidates_writes_recent_shown(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    _seed_shown(store, "Recent Paper", ["Neel Nanda"], ["interp"], "arxiv")
    _seed_shown(
        store, "Old Paper", ["X"], ["rl"], "arxiv", digest_at="2026-05-01T08:00:00Z"
    )

    out = tmp_path / "candidates.csv"
    n = export_candidates(store, since="2026-06-01T00:00:00Z", path=out)

    assert n == 1
    rows = list(csv.DictReader(out.read_text().splitlines()))
    assert rows[0]["title"] == "Recent Paper"
    assert rows[0]["picked"] == ""
    assert rows[0]["group_rating"] == ""
    store.close()


def test_import_high_rating_raises_weights(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_shown(store, "Liked Paper", ["Neel Nanda"], ["interp"], "arxiv")

    out = tmp_path / "c.csv"
    out.write_text(
        "entry_id,title,picked,group_rating,notes\n"
        f"{eid},Liked Paper,yes,5,great discussion\n"
    )
    n = import_feedback(store, path=out, week="2026-W25", alpha=0.3)

    assert n == 1
    weights = store.get_feedback_weights()
    # rating 5 -> centered +1 -> EMA from 0 with alpha 0.3 -> 0.3
    assert weights[("author", "Neel Nanda")] > 0
    assert weights[("tag", "interp")] > 0
    assert weights[("source", "arxiv")] > 0
    store.close()


def test_import_low_rating_lowers_weights(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_shown(store, "Disliked", ["Y"], ["rl"], "twitter:foo")

    out = tmp_path / "c.csv"
    out.write_text(
        "entry_id,title,picked,group_rating,notes\n" f"{eid},Disliked,yes,1,\n"
    )
    import_feedback(store, path=out, week="2026-W25", alpha=0.3)
    weights = store.get_feedback_weights()
    assert weights[("author", "Y")] < 0
    store.close()


def test_import_blank_rating_records_but_no_weight_change(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_shown(store, "Unrated", ["Z"], ["evals"], "arxiv")

    out = tmp_path / "c.csv"
    out.write_text(
        "entry_id,title,picked,group_rating,notes\n" f"{eid},Unrated,yes,,\n"
    )
    n = import_feedback(store, path=out, week="2026-W25")
    assert n == 1  # row recorded
    assert store.get_feedback_weights() == {}  # no weights touched
    store.close()
