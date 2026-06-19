"""SQLite-backed state for paper-watch.

Holds entries (deduplicated papers), per-source mentions, citation metrics,
digest history, reading-group feedback, learned feedback weights, and per-source
fetch cursors. Schema is created idempotently on open.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS entries (
        id              INTEGER PRIMARY KEY,
        arxiv_id        TEXT UNIQUE,
        doi             TEXT UNIQUE,
        title           TEXT NOT NULL,
        title_norm      TEXT NOT NULL,
        authors_json    TEXT NOT NULL DEFAULT '[]',
        abstract        TEXT,
        links_json      TEXT NOT NULL DEFAULT '{}',
        first_seen_at   TEXT NOT NULL,
        tldr            TEXT,
        why             TEXT,
        tags_json       TEXT NOT NULL DEFAULT '[]',
        safety_relevant INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entries_title_norm ON entries(title_norm)",
    """
    CREATE TABLE IF NOT EXISTS mentions (
        id              INTEGER PRIMARY KEY,
        entry_id        INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        source          TEXT NOT NULL,
        source_item_url TEXT,
        mention_text    TEXT,
        published_at    TEXT,
        fetched_at      TEXT NOT NULL,
        UNIQUE(entry_id, source, source_item_url)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        id                  INTEGER PRIMARY KEY,
        entry_id            INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        citation_count      INTEGER,
        citation_count_prev INTEGER,
        measured_at         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shown (
        id         INTEGER PRIMARY KEY,
        entry_id   INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        digest_at  TEXT NOT NULL,
        rank       INTEGER,
        score      REAL,
        resurfaced INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id           INTEGER PRIMARY KEY,
        entry_id     INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        week         TEXT NOT NULL,
        picked       INTEGER NOT NULL DEFAULT 0,
        group_rating INTEGER,
        notes        TEXT,
        imported_at  TEXT NOT NULL,
        UNIQUE(entry_id, week)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback_weights (
        key_type   TEXT NOT NULL,
        key_value  TEXT NOT NULL,
        weight     REAL NOT NULL DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (key_type, key_value)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_state (
        source          TEXT PRIMARY KEY,
        cursor          TEXT,
        last_fetched_at TEXT
    )
    """,
]


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        for stmt in SCHEMA:
            self.conn.execute(stmt)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- source cursors ----------------------------------------------------
    def get_cursor(self, source: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return row["cursor"] if row else None

    def set_cursor(self, source: str, cursor: str) -> None:
        self.conn.execute(
            """
            INSERT INTO source_state (source, cursor, last_fetched_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(source) DO UPDATE SET
                cursor = excluded.cursor,
                last_fetched_at = excluded.last_fetched_at
            """,
            (source, cursor),
        )
        self.conn.commit()

    # -- entries -----------------------------------------------------------
    def insert_entry(
        self,
        *,
        title: str,
        title_norm: str,
        first_seen_at: str,
        arxiv_id: str | None = None,
        doi: str | None = None,
        authors: list[str] | None = None,
        abstract: str | None = None,
        links: dict[str, str] | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO entries
                (arxiv_id, doi, title, title_norm, authors_json, abstract,
                 links_json, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                arxiv_id,
                doi,
                title,
                title_norm,
                json.dumps(authors or []),
                abstract,
                json.dumps(links or {}),
                first_seen_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_entry(self, entry_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()

    def get_entry_by_arxiv_id(self, arxiv_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM entries WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()

    def get_entry_by_doi(self, doi: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM entries WHERE doi = ?", (doi,)
        ).fetchone()

    def get_entry_by_title_norm(self, title_norm: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM entries WHERE title_norm = ?", (title_norm,)
        ).fetchone()
