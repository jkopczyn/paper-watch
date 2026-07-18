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
    # Every URL an entry has ever been reached by. This is the identity anchor no
    # resolver rewrites: title/title_norm/links all get clobbered when a bare-PDF
    # entry finally learns its real title, and a title-only match then misses on
    # the next run and creates the entry again -- once per run, forever. A merge
    # repoints the loser's URLs here, so the survivor keeps answering to them.
    # (mentions.source_item_url cannot serve: one Slack message linking three
    # papers writes the same permalink onto all three entries.)
    """
    CREATE TABLE IF NOT EXISTS entry_urls (
        id       INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        url      TEXT NOT NULL UNIQUE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entry_urls_entry ON entry_urls(entry_id)",
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
    """
    CREATE TABLE IF NOT EXISTS tweet_cache (
        url        TEXT PRIMARY KEY,
        text       TEXT,
        links_json TEXT NOT NULL DEFAULT '[]',
        quoted_url TEXT,
        thread_url TEXT,
        status     TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
]

# Key under which the ISO timestamp of the last completed (non-dry) run is stored
# in the `meta` table, so a run can widen its fetch window to cover any gap left
# by the machine being powered off. See runtime.effective_since.
LAST_RUN_KEY = "last_run_at"


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
        # Enrichment v2: 0-10 relevance vs the reader profile, plus a schema
        # version so old enrichments are redone lazily.
        self._add_column_if_missing("entries", "relevance", "INTEGER")
        self._add_column_if_missing("entries", "enrich_version", "INTEGER")
        # Best known *real* publication date of the paper (ISO-8601 Z), populated
        # only from authoritative metadata resolution (arXiv API / S2 / Crossref);
        # NULL means "unknown, estimate from mentions/first_seen at display time".
        self._add_column_if_missing("entries", "published_at", "TEXT")
        self.conn.commit()

    def _add_column_if_missing(self, table: str, column: str, decl: str) -> None:
        cols = {
            r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.conn.close()

    # -- key/value meta ----------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_last_run_at(self) -> str | None:
        return self.get_meta(LAST_RUN_KEY)

    def set_last_run_at(self, iso: str) -> None:
        self.set_meta(LAST_RUN_KEY, iso)

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

    # -- tweet resolution cache --------------------------------------------
    def get_tweet_cache(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tweet_cache WHERE url = ?", (url,)
        ).fetchone()

    def set_tweet_cache(
        self,
        url: str,
        *,
        text: str | None,
        links: list[str],
        quoted_url: str | None,
        thread_url: str | None,
        status: str,
        fetched_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO tweet_cache
                (url, text, links_json, quoted_url, thread_url, status, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (url, text, json.dumps(links), quoted_url, thread_url, status, fetched_at),
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
        source_url: str | None = None,
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
        entry_id = int(cur.lastrowid)
        if source_url:
            self.add_entry_url(entry_id, source_url, commit=False)
        self.conn.commit()
        return entry_id

    def add_entry_url(self, entry_id: int, url: str, *, commit: bool = True) -> None:
        """Record a URL this entry answers to. Ignored if another entry owns it."""
        self.conn.execute(
            "INSERT OR IGNORE INTO entry_urls (entry_id, url) VALUES (?, ?)",
            (entry_id, url),
        )
        if commit:
            self.conn.commit()

    def get_entry_by_source_url(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT e.* FROM entries e JOIN entry_urls u ON u.entry_id = e.id "
            "WHERE u.url = ?",
            (url,),
        ).fetchone()

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

    def find_twin_entry_id(
        self,
        entry_id: int,
        *,
        arxiv_id: str | None,
        doi: str | None,
        title_norm: str | None,
    ) -> int | None:
        """The oldest *other* entry that is the same paper as `entry_id`, if any."""
        clauses, params = [], []
        if arxiv_id:
            clauses.append("arxiv_id = ?")
            params.append(arxiv_id)
        if doi:
            clauses.append("doi = ?")
            params.append(doi)
        if title_norm:
            clauses.append("title_norm = ?")
            params.append(title_norm)
        if not clauses:
            return None
        params.append(entry_id)
        row = self.conn.execute(
            f"SELECT id FROM entries WHERE ({' OR '.join(clauses)}) AND id != ? "
            "ORDER BY id LIMIT 1",
            params,
        ).fetchone()
        return int(row["id"]) if row else None

    def merge_entries(self, *, winner_id: int, loser_id: int) -> None:
        """Fold `loser_id` into `winner_id` and delete it.

        Mentions, metrics, shown and feedback rows are repointed at the winner;
        `OR IGNORE` drops the ones that would collide with a row the winner
        already has (both entries seen in the same source, or shown in the same
        digest). Identity fields the winner is missing are adopted from the loser,
        so merging never loses an arXiv ID or an abstract.
        """
        if winner_id == loser_id:
            return
        winner = self.get_entry(winner_id)
        loser = self.get_entry(loser_id)
        if winner is None or loser is None:
            return

        # entry_urls included: the survivor must keep answering to the loser's
        # URLs, or the next run re-creates it from one of them and merges it away
        # again, every run.
        for table in ("mentions", "metrics", "shown", "feedback", "entry_urls"):
            self.conn.execute(
                f"UPDATE OR IGNORE {table} SET entry_id = ? WHERE entry_id = ?",
                (winner_id, loser_id),
            )

        adopted = {
            col: loser[col]
            for col in ("arxiv_id", "doi", "abstract", "relevance", "published_at")
            if winner[col] is None and loser[col] is not None
        }
        merged_links = json.loads(winner["links_json"])
        for key, value in json.loads(loser["links_json"]).items():
            merged_links.setdefault(key, value)
        adopted["links_json"] = json.dumps(merged_links)

        # Drop the loser before the winner adopts its identity: arxiv_id is
        # UNIQUE, so the two cannot both hold it even for one statement.
        # ON DELETE CASCADE takes the child rows that OR IGNORE left behind.
        self.conn.execute("DELETE FROM entries WHERE id = ?", (loser_id,))
        assignments = ", ".join(f"{col} = ?" for col in adopted)
        self.conn.execute(
            f"UPDATE entries SET {assignments} WHERE id = ?",
            (*adopted.values(), winner_id),
        )
        self.conn.commit()

    def update_paper_metadata(
        self,
        entry_id: int,
        *,
        title: str,
        title_norm: str,
        authors: list[str],
        abstract: str | None,
        links: dict[str, str],
        published_at: str | None = None,
    ) -> None:
        """Replace a post-shaped entry's identity fields with the real paper's.

        `links` entries are merged over the existing ones (the post URL lives on
        in mentions; the entry should link the paper). `published_at` is set only
        when given, so a later resolve that doesn't carry a date never wipes a
        date an earlier resolve already learned.
        """
        row = self.get_entry(entry_id)
        if row is None:
            return
        merged = json.loads(row["links_json"])
        merged.update(links)
        cols = ["title = ?", "title_norm = ?", "authors_json = ?", "abstract = ?", "links_json = ?"]
        params: list[Any] = [title, title_norm, json.dumps(authors), abstract, json.dumps(merged)]
        if published_at is not None:
            cols.append("published_at = ?")
            params.append(published_at)
        params.append(entry_id)
        self.conn.execute(
            f"UPDATE entries SET {', '.join(cols)} WHERE id = ?", params
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
    def get_unenriched(self, limit: int, *, version: int = 1) -> list[sqlite3.Row]:
        """Entries lacking enrichment at `version` (never enriched, or older)."""
        return self.conn.execute(
            "SELECT * FROM entries "
            "WHERE enrich_version IS NULL OR enrich_version < ? "
            "ORDER BY id LIMIT ?",
            (version, limit),
        ).fetchall()

    def set_enrichment(
        self,
        entry_id: int,
        *,
        tldr: str,
        why: str,
        tags: list[str],
        relevance: int,
        version: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE entries
            SET tldr = ?, why = ?, tags_json = ?, relevance = ?, enrich_version = ?
            WHERE id = ?
            """,
            (tldr, why, json.dumps(tags), relevance, version, entry_id),
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

    def count_shown_since(self, entry_id: int, since: str) -> int:
        """How many past digests surfaced this entry at/after `since`."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM shown WHERE entry_id = ? AND digest_at >= ?",
            (entry_id, since),
        ).fetchone()
        return int(row["n"])

    def earliest_published_at(self, entry_id: int) -> str | None:
        """Earliest non-null `published_at` across this entry's mentions, if any.

        Used to estimate a paper's publication date when no authoritative date is
        stored on the entry: for an arXiv-sourced mention this is the real submit
        date; for a tweet/blog it's the post date, hence shown as an estimate.
        """
        row = self.conn.execute(
            "SELECT MIN(published_at) AS p FROM mentions "
            "WHERE entry_id = ? AND published_at IS NOT NULL",
            (entry_id,),
        ).fetchone()
        return row["p"] if row and row["p"] else None

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

    def count_feedback_weeks(self) -> int:
        """Distinct ISO weeks that have any imported feedback (drives the
        dynamic feedback weight)."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT week) AS n FROM feedback"
        ).fetchone()
        return int(row["n"]) if row else 0

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

    def count_mention_occasions_since(self, entry_id: int, since: str) -> int:
        """Distinct (source, day) pairs mentioning `entry_id` since `since`.

        One source flagging a paper on one day is one occasion however many links
        it used — an Alignment Forum post that links the post, the arXiv abs and
        the PDF is one act of attention, not three. Raw mention rows would count
        it three times and read as a surge.
        """
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT source || '|' || substr(fetched_at, 1, 10)) AS n "
            "FROM mentions WHERE entry_id = ? AND fetched_at >= ?",
            (entry_id, since),
        ).fetchone()
        return int(row["n"])

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

    # -- offline eval helpers (historical replay) ---------------------------
    def entry_ids_mentioned_between(self, start: str, end: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT entry_id FROM mentions "
            "WHERE fetched_at >= ? AND fetched_at <= ? ORDER BY entry_id",
            (start, end),
        ).fetchall()
        return [int(r["entry_id"]) for r in rows]

    def count_mentions_between(self, entry_id: int, start: str, end: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM mentions "
            "WHERE entry_id = ? AND fetched_at >= ? AND fetched_at <= ?",
            (entry_id, start, end),
        ).fetchone()
        return int(row["n"])

    def get_entry_id_by_mention_url(self, url: str) -> int | None:
        row = self.conn.execute(
            "SELECT entry_id FROM mentions WHERE source_item_url = ? LIMIT 1",
            (url,),
        ).fetchone()
        return int(row["entry_id"]) if row else None
