from pathlib import Path

from paper_watch.store import Store

EXPECTED_TABLES = {
    "entries",
    "entry_urls",
    "mentions",
    "metrics",
    "shown",
    "feedback",
    "feedback_weights",
    "source_state",
    "meta",
}


def _table_names(store: Store) -> set[str]:
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_migrate_creates_all_tables(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    assert EXPECTED_TABLES <= _table_names(store)
    store.close()


def test_migrate_is_idempotent(tmp_path: Path):
    db = tmp_path / "pw.db"
    Store(db).close()
    # opening again should not raise
    store = Store(db)
    assert EXPECTED_TABLES <= _table_names(store)
    store.close()


def test_source_state_roundtrip(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    assert store.get_cursor("arxiv") is None

    store.set_cursor("arxiv", "2026-06-19T00:00:00Z")
    assert store.get_cursor("arxiv") == "2026-06-19T00:00:00Z"

    # update overwrites
    store.set_cursor("arxiv", "2026-06-20T00:00:00Z")
    assert store.get_cursor("arxiv") == "2026-06-20T00:00:00Z"
    store.close()


def test_meta_roundtrip_and_upsert(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    assert store.get_meta("k") is None
    store.set_meta("k", "v1")
    assert store.get_meta("k") == "v1"
    store.set_meta("k", "v2")  # upsert overwrites
    assert store.get_meta("k") == "v2"
    store.close()


def test_last_run_at_roundtrip(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    assert store.get_last_run_at() is None
    store.set_last_run_at("2026-06-19T09:00:00Z")
    assert store.get_last_run_at() == "2026-06-19T09:00:00Z"
    store.close()


def test_insert_and_fetch_entry(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    entry_id = store.insert_entry(
        title="Scalable Oversight",
        title_norm="scalable oversight",
        arxiv_id="2406.00001",
        authors=["Ethan Perez"],
        abstract="An abstract.",
        links={"abstract": "https://arxiv.org/abs/2406.00001"},
        first_seen_at="2026-06-19T00:00:00Z",
    )
    row = store.get_entry(entry_id)
    assert row["title"] == "Scalable Oversight"
    assert row["arxiv_id"] == "2406.00001"
    assert store.get_entry_by_arxiv_id("2406.00001")["id"] == entry_id
    store.close()


def test_add_mention_is_idempotent_and_counts_sources(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="T", title_norm="t", first_seen_at="2026-06-19T00:00:00Z"
    )
    url = "https://arxiv.org/abs/2406.00001"
    first = store.add_mention(
        entry_id=eid, source="arxiv", source_item_url=url, fetched_at="2026-06-19T00:00:00Z"
    )
    dup = store.add_mention(
        entry_id=eid, source="arxiv", source_item_url=url, fetched_at="2026-06-19T01:00:00Z"
    )
    assert first is not None
    assert dup is None  # ignored duplicate

    store.add_mention(
        entry_id=eid,
        source="rss:Blog",
        source_item_url="https://blog/p",
        fetched_at="2026-06-19T00:00:00Z",
    )
    assert store.count_distinct_sources(eid) == 2
    assert len(store.get_mentions(eid)) == 2
    store.close()


def test_entry_has_trusted_mention(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="T", title_norm="t", first_seen_at="2026-06-19T00:00:00Z"
    )
    # default mention is untrusted
    store.add_mention(
        entry_id=eid, source="rss:Blog", source_item_url="https://blog/p",
        fetched_at="2026-06-19T00:00:00Z",
    )
    assert store.entry_has_trusted_mention(eid) is False

    # a trusted slack mention flips it
    store.add_mention(
        entry_id=eid, source="slack:mats:papers", source_item_url="https://arxiv.org/abs/1",
        fetched_at="2026-06-19T00:00:00Z", trusted=True,
    )
    assert store.entry_has_trusted_mention(eid) is True
    store.close()


def test_entries_have_published_at_column_defaulting_null(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="T", title_norm="t", first_seen_at="2026-06-19T00:00:00Z"
    )
    assert store.get_entry(eid)["published_at"] is None
    store.close()


def test_update_paper_metadata_sets_published_at(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="tweet text", title_norm="tweet text",
        first_seen_at="2026-06-19T00:00:00Z",
    )
    store.update_paper_metadata(
        eid, title="Impossibility Results", title_norm="impossibility results",
        authors=["A"], abstract="x", links={"abstract": "https://arxiv.org/abs/1810.1"},
        published_at="2018-10-01T00:00:00Z",
    )
    assert store.get_entry(eid)["published_at"] == "2018-10-01T00:00:00Z"
    store.close()


def test_update_paper_metadata_keeps_published_at_when_not_given(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="t", title_norm="t", first_seen_at="2026-06-19T00:00:00Z"
    )
    store.update_paper_metadata(
        eid, title="Real", title_norm="real", authors=[], abstract="x",
        links={}, published_at="2018-01-01T00:00:00Z",
    )
    # A later resolve with no date must not wipe the known one.
    store.update_paper_metadata(
        eid, title="Real", title_norm="real", authors=[], abstract="y", links={},
    )
    assert store.get_entry(eid)["published_at"] == "2018-01-01T00:00:00Z"
    store.close()


def test_count_shown_since_windows_by_digest_time(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="t", title_norm="t", first_seen_at="2026-06-19T00:00:00Z"
    )
    for at in ("2026-07-01T00:00:00Z", "2026-07-10T08:00:00Z", "2026-07-11T08:00:00Z"):
        store.record_shown(entry_id=eid, digest_at=at, rank=1, score=1.0, resurfaced=False)
    assert store.count_shown_since(eid, "2026-07-10T00:00:00Z") == 2
    assert store.count_shown_since(eid, "2026-06-01T00:00:00Z") == 3
    assert store.count_shown_since(eid, "2026-08-01T00:00:00Z") == 0
    store.close()


def test_merge_adopts_published_at_when_winner_lacks_it(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    winner = store.insert_entry(
        title="P", title_norm="p", first_seen_at="2026-07-01T00:00:00Z"
    )
    loser = store.insert_entry(
        title="P", title_norm="p", first_seen_at="2026-07-02T00:00:00Z"
    )
    store.update_paper_metadata(
        loser, title="P", title_norm="p", authors=[], abstract="x", links={},
        published_at="2018-01-01T00:00:00Z",
    )
    store.merge_entries(winner_id=winner, loser_id=loser)
    assert store.get_entry(winner)["published_at"] == "2018-01-01T00:00:00Z"
    store.close()


def test_merge_entries_repoints_mentions_metrics_and_shown(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    winner = store.insert_entry(
        title="Real Paper", title_norm="real paper", first_seen_at="2026-07-01T00:00:00Z"
    )
    loser = store.insert_entry(
        title="Real Paper", title_norm="real paper", first_seen_at="2026-07-02T00:00:00Z"
    )
    store.add_mention(
        entry_id=loser, source="rss", fetched_at="2026-07-02T00:00:00Z",
        source_item_url="https://example.org/a",
    )
    store.record_metrics(loser, 12, "2026-07-02T00:00:00Z")
    store.record_shown(
        entry_id=loser, digest_at="2026-07-02T00:00:00Z", rank=1, score=1.0,
        resurfaced=False,
    )

    store.merge_entries(winner_id=winner, loser_id=loser)

    assert store.get_entry(loser) is None
    assert [m["source_item_url"] for m in store.get_mentions(winner)] == [
        "https://example.org/a"
    ]
    assert store.latest_metrics(winner)["citation_count"] == 12
    assert store.was_shown(winner)
    store.close()


def test_merge_entries_tolerates_a_mention_both_entries_share(tmp_path: Path):
    # The UNIQUE(entry_id, source, source_item_url) constraint must not blow up
    # when the loser carries a mention the winner already has.
    store = Store(tmp_path / "pw.db")
    winner = store.insert_entry(
        title="P", title_norm="p", first_seen_at="2026-07-01T00:00:00Z"
    )
    loser = store.insert_entry(
        title="P", title_norm="p", first_seen_at="2026-07-02T00:00:00Z"
    )
    for eid in (winner, loser):
        store.add_mention(
            entry_id=eid, source="rss", fetched_at="2026-07-02T00:00:00Z",
            source_item_url="https://example.org/same",
        )
    store.merge_entries(winner_id=winner, loser_id=loser)
    assert store.get_entry(loser) is None
    assert len(store.get_mentions(winner)) == 1
    store.close()
