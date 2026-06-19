from pathlib import Path

from paper_watch.store import Store

EXPECTED_TABLES = {
    "entries",
    "mentions",
    "metrics",
    "shown",
    "feedback",
    "feedback_weights",
    "source_state",
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
