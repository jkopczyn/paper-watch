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
        self._add_column_if_missing(
            "mentions", "trusted", "INTEGER NOT NULL DEFAULT 0"
        )
        self.conn.commit()

    def _add_column_if_missing(self, table: str, column: str, decl: str) -> None:
        cols = {
            r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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

    def update_paper_metadata(
        self,
        entry_id: int,
        *,
        title: str,
        title_norm: str,
        authors: list[str],
        abstract: str | None,
        links: dict[str, str],
    ) -> None:
        """Replace a post-shaped entry's identity fields with the real paper's.

        `links` entries are merged over the existing ones (the post URL lives on
        in mentions; the entry should link the paper).
        """
        row = self.get_entry(entry_id)
        if row is None:
            return
        merged = json.loads(row["links_json"])
        merged.update(links)
        self.conn.execute(
            """
            UPDATE entries
            SET title = ?, title_norm = ?, authors_json = ?, abstract = ?,
                links_json = ?
            WHERE id = ?
            """,
            (title, title_norm, json.dumps(authors), abstract, json.dumps(merged), entry_id),
        )
        self.conn.commit()

    # -- mentions ----------------------------------------------------------
    def add_mention(
        self,
        *,
        entry_id: int,
        source: str,
        fetched_at: str,
        source_item_url: str | None = None,
        mention_text: str | None = None,
        published_at: str | None = None,
        trusted: bool = False,
    ) -> int | None:
        """Record one source appearance. Idempotent on (entry, source, url).

        `trusted` marks a curated mention (e.g. a trusted Slack channel or a
        Slack link to a known paper domain) that bypasses the relevance gate.
        Returns the new row id, or None if this mention already existed.
        """
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO mentions
                (entry_id, source, source_item_url, mention_text,
                 published_at, fetched_at, trusted)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                source,
                source_item_url,
                mention_text,
                published_at,
                fetched_at,
                1 if trusted else 0,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid) if cur.rowcount else None

    def entry_has_trusted_mention(self, entry_id: int) -> bool:
        """True if any mention of this entry was recorded as trusted."""
        row = self.conn.execute(
            "SELECT 1 FROM mentions WHERE entry_id = ? AND trusted = 1 LIMIT 1",
            (entry_id,),
        ).fetchone()
        return row is not None

    def get_mentions(self, entry_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM mentions WHERE entry_id = ? ORDER BY id", (entry_id,)
        ).fetchall()

    # -- enrichment --------------------------------------------------------
    def get_unenriched(self, limit: int) -> list[sqlite3.Row]:
        """Entries that have not been LLM-enriched yet (tldr IS NULL)."""
        return self.conn.execute(
            "SELECT * FROM entries WHERE tldr IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def set_enrichment(
        self,
        entry_id: int,
        *,
        tldr: str,
        why: str,
        tags: list[str],
        safety_relevant: bool,
    ) -> None:
        self.conn.execute(
            """
            UPDATE entries
            SET tldr = ?, why = ?, tags_json = ?, safety_relevant = ?
            WHERE id = ?
            """,
            (tldr, why, json.dumps(tags), 1 if safety_relevant else 0, entry_id),
        )
        self.conn.commit()

    def count_distinct_sources(self, entry_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT source) AS n FROM mentions WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        return int(row["n"])

    # -- digest history ----------------------------------------------------
    def record_shown(
        self,
        *,
        entry_id: int,
        digest_at: str,
        rank: int,
        score: float,
        resurfaced: bool,
    ) -> None:
        self.conn.execute(
            "INSERT INTO shown (entry_id, digest_at, rank, score, resurfaced) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry_id, digest_at, rank, score, 1 if resurfaced else 0),
        )
        self.conn.commit()

    def was_shown(self, entry_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM shown WHERE entry_id = ? LIMIT 1", (entry_id,)
        ).fetchone()
        return row is not None

    def entries_shown_since(self, since: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT DISTINCT e.id, e.title, e.authors_json, e.tags_json
            FROM shown s JOIN entries e ON e.id = s.entry_id
            WHERE s.digest_at >= ?
            ORDER BY e.id
            """,
            (since,),
        ).fetchall()

    # -- feedback ----------------------------------------------------------
    def record_feedback(
        self,
        *,
        entry_id: int,
        week: str,
        picked: bool,
        group_rating: int | None,
        notes: str | None,
        imported_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO feedback
                (entry_id, week, picked, group_rating, notes, imported_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id, week) DO UPDATE SET
                picked = excluded.picked,
                group_rating = excluded.group_rating,
                notes = excluded.notes,
                imported_at = excluded.imported_at
            """,
            (entry_id, week, 1 if picked else 0, group_rating, notes, imported_at),
        )
        self.conn.commit()

    def get_feedback_weight(self, key_type: str, key_value: str) -> float:
        row = self.conn.execute(
            "SELECT weight FROM feedback_weights WHERE key_type = ? AND key_value = ?",
            (key_type, key_value),
        ).fetchone()
        return float(row["weight"]) if row else 0.0

    def set_feedback_weight(self, key_type: str, key_value: str, weight: float) -> None:
        self.conn.execute(
            """
            INSERT INTO feedback_weights (key_type, key_value, weight, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(key_type, key_value) DO UPDATE SET
                weight = excluded.weight, updated_at = excluded.updated_at
            """,
            (key_type, key_value, weight),
        )
        self.conn.commit()

    def get_feedback_weights(self) -> dict[tuple[str, str], float]:
        rows = self.conn.execute(
            "SELECT key_type, key_value, weight FROM feedback_weights"
        ).fetchall()
        return {(r["key_type"], r["key_value"]): float(r["weight"]) for r in rows}

    # -- metrics / windows (for velocity & candidacy) ----------------------
    def record_metrics(self, entry_id: int, citation_count: int, measured_at: str) -> None:
        prev = self.conn.execute(
            "SELECT citation_count FROM metrics WHERE entry_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        prev_count = prev["citation_count"] if prev else None
        self.conn.execute(
            "INSERT INTO metrics (entry_id, citation_count, citation_count_prev, measured_at) "
            "VALUES (?, ?, ?, ?)",
            (entry_id, citation_count, prev_count, measured_at),
        )
        self.conn.commit()

    def latest_metrics(self, entry_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT citation_count, citation_count_prev FROM metrics "
            "WHERE entry_id = ? ORDER BY id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()

    def count_mentions_since(self, entry_id: int, since: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM mentions WHERE entry_id = ? AND fetched_at >= ?",
            (entry_id, since),
        ).fetchone()
        return int(row["n"])

    def active_entry_ids_since(self, since: str) -> list[int]:
        """Entries with at least one mention fetched at/after `since`."""
        rows = self.conn.execute(
            "SELECT DISTINCT entry_id FROM mentions WHERE fetched_at >= ? ORDER BY entry_id",
            (since,),
        ).fetchall()
        return [int(r["entry_id"]) for r in rows]
