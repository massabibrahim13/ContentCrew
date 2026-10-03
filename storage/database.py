"""
SQLite persistence.

SQLite is enough for a classroom app: one file, no server, and it handles the
small amount of concurrency we need (the API thread writing events while the
browser's live stream reads them). WAL mode lets readers and a writer work at
the same time.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

SCHEMA = """
-- The company the agents write for. Exactly one row (id = 1).
CREATE TABLE IF NOT EXISTS company_profile (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    name            TEXT NOT NULL,
    website         TEXT,
    industry        TEXT,
    description     TEXT NOT NULL,
    target_audience TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
    products        TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competitors (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    website  TEXT,
    position INTEGER NOT NULL DEFAULT 0
);

-- One row per conversation / workflow run. This is the app-level view of the
-- workflow that the UI and /api/status read. (From Phase 3, LangGraph keeps its
-- own detailed checkpoint state; this table mirrors the parts the UI needs.)
CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    status            TEXT NOT NULL,
    current_agent     TEXT,
    current_node      TEXT,
    approval_required TEXT,                       -- 'research' | 'content' | NULL
    research_approved INTEGER NOT NULL DEFAULT 0,
    content_approved  INTEGER NOT NULL DEFAULT 0,
    blog_status       TEXT NOT NULL DEFAULT 'none',
    publish_status    TEXT NOT NULL DEFAULT 'none',
    last_error        TEXT,
    graph_thread_id   TEXT,                       -- LangGraph thread for the current run
    current_blog_id   TEXT,                       -- the newest draft: the only one that can be published
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Everything the timeline shows: user messages, workflow steps, agent and
-- tool activity, approval requests. Ordered by id.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,                     -- JSON object
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id, id);

CREATE TABLE IF NOT EXISTS blogs (
    id           TEXT PRIMARY KEY,
    session_id   TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,                   -- Markdown
    status       TEXT NOT NULL,                   -- 'draft' | 'published'
    slug         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    published_at TEXT
);

-- Written by the Analysis Agent (Phase 3); feeds the "Research" numbers in
-- the left panel.
CREATE TABLE IF NOT EXISTS research_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    articles_analyzed   INTEGER NOT NULL DEFAULT 0,
    keywords_discovered INTEGER NOT NULL DEFAULT 0,
    topic               TEXT,                     -- Phase 6: shown with the latest summary
    summary             TEXT,                     -- Phase 6: the research summary, in plain words
    created_at          TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Opens short-lived connections to one SQLite file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
        log.info("Database ready at %s", self.path)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created (keeps existing data)."""
        added = {
            # Phase 3: which LangGraph thread (checkpoint history) belongs to the chat's current run
            ("sessions", "graph_thread_id"): "TEXT",
            # Phase 7: which draft is current (older versions can't be edited or published)
            ("sessions", "current_blog_id"): "TEXT",
            # Phase 6: the latest research summary in the left panel
            ("research_runs", "topic"): "TEXT",
            ("research_runs", "summary"): "TEXT",
        }
        for (table, column), kind in added.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                log.info("Database updated: added %s.%s", table, column)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and rolls back on error."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
