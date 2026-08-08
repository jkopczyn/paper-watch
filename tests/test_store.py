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


def test_last_sent_at_is_tracked_separately_from_last_run(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    assert store.get_last_sent_at() is None
    store.set_last_run_at("2026-08-05T08:00:00Z")
    # ingest-only ticks move last_run_at; the delivery watermark stays put
    assert store.get_last_sent_at() is None
    store.set_last_sent_at("2026-08-07T12:00:00Z")
    assert store.get_last_sent_at() == "2026-08-07T12:00:00Z"
    assert store.get_last_run_at() == "2026-08-05T08:00:00Z"
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


def test_source_health_counts_consecutive_failures(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    key = "page:https://transluce.org/news"
    assert store.unhealthy_sources(1) == []

    for _ in range(3):
        store.record_source_failure(
            key, label="Transluce", error="404 Not Found", at="2026-08-05T08:00:00Z"
        )
    (row,) = store.unhealthy_sources(1)
    assert row["label"] == "Transluce"
    assert row["consecutive_failures"] == 3
    assert row["last_error"] == "404 Not Found"
    store.close()


def test_a_success_clears_the_failure_streak(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    key = "page:https://x.example/blog"
    store.record_source_failure(key, label="X", error="boom", at="2026-08-05T08:00:00Z")
    store.record_source_failure(key, label="X", error="boom", at="2026-08-05T12:00:00Z")
    store.record_source_ok(key, label="X", at="2026-08-05T16:00:00Z")

    assert store.unhealthy_sources(1) == []
    row = store.get_source_health(key)
    assert row["consecutive_failures"] == 0
    assert row["last_ok_at"] == "2026-08-05T16:00:00Z"
    store.close()


def test_unhealthy_sources_respects_the_threshold(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    # A transient blip must not raise an alarm the way a dead URL does.
    store.record_source_failure("page:a", label="Blip", error="429", at="2026-08-05T08:00:00Z")
    for _ in range(4):
        store.record_source_failure("page:b", label="Dead", error="404", at="2026-08-05T08:00:00Z")

    assert [r["label"] for r in store.unhealthy_sources(3)] == ["Dead"]
    assert {r["label"] for r in store.unhealthy_sources(1)} == {"Blip", "Dead"}
    store.close()


def test_source_health_reports_a_source_that_never_worked(tmp_path: Path):
    store = Store(tmp_path / "pw.db")
    store.record_source_failure("page:c", label="Typo'd", error="404", at="2026-08-05T08:00:00Z")
    (row,) = store.unhealthy_sources(1)
    # Never succeeded, so there is no "healthy since" date to show.
    assert row["last_ok_at"] is None
    store.close()


# -- as-of view (historical replay) ----------------------------------------
def _replay_fixture(tmp_path: Path):
    """One entry mentioned and shown across three days, for clock-freeze tests."""
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="A Paper", title_norm="a paper", first_seen_at="2026-08-01T00:00:00Z"
    )
    for day, n in (("01", 1), ("03", 2), ("07", 3)):
        for i in range(n):
            store.add_mention(
                entry_id=eid,
                source=f"rss:S{i}",
                source_item_url=f"https://ex.com/p{day}{i}",
                fetched_at=f"2026-08-{day}T0{i}:00:00Z",
            )
    store.record_shown(entry_id=eid, digest_at="2026-08-02T12:00:00Z", rank=1, score=1.0, resurfaced=False)
    store.record_shown(entry_id=eid, digest_at="2026-08-06T12:00:00Z", rank=1, score=1.0, resurfaced=True)
    return store, eid


def test_between_counts_exclude_what_came_after_the_bound(tmp_path: Path):
    store, eid = _replay_fixture(tmp_path)
    assert store.count_mentions_between(eid, "2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z") == 3
    assert store.count_mention_occasions_between(eid, "2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z") == 3
    # the 08-07 mentions exist in the store but are after the bound
    assert store.count_mentions_between(eid, "2026-08-02T00:00:00Z", "2026-08-05T00:00:00Z") == 2
    store.close()


def test_shown_state_before_a_bound(tmp_path: Path):
    store, eid = _replay_fixture(tmp_path)
    assert store.was_shown_before(eid, "2026-08-02T12:00:00Z") is False  # strict: not its own digest
    assert store.was_shown_before(eid, "2026-08-03T00:00:00Z") is True
    # count_shown_between is strict at the upper bound too (the replayed digest
    # must not count itself)
    assert store.count_shown_between(eid, "2026-08-01T00:00:00Z", "2026-08-06T12:00:00Z") == 1
    assert store.count_shown_between(eid, "2026-08-01T00:00:00Z", "2026-08-07T00:00:00Z") == 2
    store.close()


def test_last_digest_before(tmp_path: Path):
    store, _ = _replay_fixture(tmp_path)
    assert store.last_digest_before("2026-08-01T00:00:00Z") is None
    assert store.last_digest_before("2026-08-06T12:00:00Z") == "2026-08-02T12:00:00Z"  # strict
    assert store.last_digest_before("2026-08-08T00:00:00Z") == "2026-08-06T12:00:00Z"
    store.close()


def test_as_of_view_freezes_the_clock_and_reads_the_rest_through(tmp_path: Path):
    from paper_watch.store import AsOfStoreView

    store, eid = _replay_fixture(tmp_path)
    view = AsOfStoreView(store, "2026-08-06T12:00:00Z")

    assert view.count_mentions_since(eid, "2026-08-01T00:00:00Z") == 3
    assert view.count_mention_occasions_since(eid, "2026-08-01T00:00:00Z") == 3
    assert view.active_entry_ids_since("2026-08-01T00:00:00Z") == [eid]
    assert view.active_entry_ids_since("2026-08-05T00:00:00Z") == []
    # the 08-06 digest is the one being replayed: not "shown", not the watermark
    assert view.was_shown(eid) is True   # 08-02 was
    assert view.count_shown_since(eid, "2026-08-01T00:00:00Z") == 1
    assert view.get_last_sent_at() == "2026-08-02T12:00:00Z"
    # non-time-anchored reads pass through to the real store
    assert view.get_entry(eid)["title"] == "A Paper"
    store.close()
