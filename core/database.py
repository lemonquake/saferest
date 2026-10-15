"""
Shared SQLite store for onboarding logs, growth snapshots, live reports and
the saved embed template library.
Created by Aljay Leodones for SafeRest.

The bot keeps one small asynchronous wrapper around `sqlite3` (executed on a
worker thread) so no extra dependency is required. WAL journaling keeps reads
fast while the engagement tracker writes in the background.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sqlite3
from typing import Any, Iterable, NamedTuple, Sequence

logger = logging.getLogger("saferest_bot.database")

SCHEMA = """
-- ── Onboarding tracking ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS onboarded_members (
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    onboarded_at TEXT DEFAULT (datetime('now')),
    onboarded_by INTEGER,
    method       TEXT DEFAULT 'panel',
    PRIMARY KEY (user_id, guild_id)
);

-- ── Creator onboarding (TikTok Shop question) ───────────────
-- A separate log from `onboarded_members`, so a member welcomed by
-- Information Onboarding still shows up in the creator onboarding scan.
CREATE TABLE IF NOT EXISTS creator_onboarded_members (
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    onboarded_at TEXT DEFAULT (datetime('now')),
    onboarded_by INTEGER,
    PRIMARY KEY (user_id, guild_id)
);

-- The latest answers each member gave to the creator questions.
CREATE TABLE IF NOT EXISTS creator_onboarding_answers (
    user_id    INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    is_creator INTEGER NOT NULL,          -- 1 = YES, 0 = NO
    journey    TEXT,                      -- 'learning' | 'experienced'
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, guild_id)
);

-- ── SafeRest Onboarding (private per-member channels) ────────────
CREATE TABLE IF NOT EXISTS saferest_onboarding_members (
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER,                 -- their private <handle>-onboarding channel
    batch_id     INTEGER,                 -- the send that tagged them; NULL = not tagged
    tagged_at    REAL,                    -- unix time of that send
    tagged_by    INTEGER,
    completed_at REAL,                    -- questionnaire done, access role granted
    PRIMARY KEY (user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_saferest_onboarding_channel
    ON saferest_onboarding_members (guild_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_saferest_onboarding_batch
    ON saferest_onboarding_members (guild_id, batch_id);

-- ── Daily member snapshots (growth baselines) ───────────────
CREATE TABLE IF NOT EXISTS member_snapshots (
    guild_id      INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,          -- 'YYYY-MM-DD' (UTC)
    total         INTEGER NOT NULL,
    humans        INTEGER NOT NULL,
    bots          INTEGER NOT NULL,
    online        INTEGER NOT NULL DEFAULT 0,
    admins        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, snapshot_date)
);

-- ── Join tally (daily + weekly buckets) ─────────────────────
CREATE TABLE IF NOT EXISTS member_joins (
    guild_id    INTEGER NOT NULL,
    period      TEXT NOT NULL,            -- 'day' or 'week'
    period_date TEXT NOT NULL,            -- 'YYYY-MM-DD' (UTC; week = Monday)
    joins       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, period, period_date)
);

-- ── Deployed self-refreshing public reports ─────────────────
CREATE TABLE IF NOT EXISTS member_reports (
    guild_id    INTEGER NOT NULL,
    timeframe   TEXT NOT NULL,            -- 'daily' / 'weekly' / 'monthly' / 'custom'
    days        INTEGER NOT NULL DEFAULT 1,
    label       TEXT NOT NULL DEFAULT '',
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, timeframe)
);

-- ── Per-server settings chosen from the Mod Panel ───────────
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT,
    PRIMARY KEY (guild_id, key)
);

-- ── Saved embed templates (Embed Studio library) ────────────
CREATE TABLE IF NOT EXISTS embed_templates (
    template_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    name         TEXT NOT NULL,          -- name exactly as the moderator typed it
    name_key     TEXT NOT NULL,          -- casefolded name; enforces uniqueness
    payload      TEXT NOT NULL,          -- JSON embed payload (see core.embed_script)
    created_by   INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by   INTEGER,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    use_count    INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT
);

-- One template name per server, compared case-insensitively, so "Restock"
-- and "restock" can never both exist and confuse the picker.
CREATE UNIQUE INDEX IF NOT EXISTS embed_templates_name_idx
    ON embed_templates (guild_id, name_key);

CREATE INDEX IF NOT EXISTS embed_templates_guild_idx
    ON embed_templates (guild_id, updated_at DESC);

-- ── Weekly announcement schedules (SafeRest Discord Announcement) ─
CREATE TABLE IF NOT EXISTS announcement_schedules (
    guild_id         INTEGER NOT NULL,
    slug             TEXT NOT NULL,          -- 'monday_kickoff', 'wednesday_checkin', ...
    channel_id       INTEGER NOT NULL,
    weekday          INTEGER NOT NULL,       -- 0 = Monday ... 6 = Sunday
    hour             INTEGER NOT NULL,
    minute           INTEGER NOT NULL DEFAULT 0,
    mention_everyone INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run         TEXT,                   -- 'YYYY-MM-DD' in the report timezone
    updated_by       INTEGER,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, slug)
);

-- ── Auto-saved composer drafts (one per moderator per server) ─
CREATE TABLE IF NOT EXISTS embed_drafts (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, user_id)
);
"""


class WriteResult(NamedTuple):
    """Outcome of a single write, so callers can tell a hit from a no-op."""

    rowcount: int
    lastrowid: int | None


class CommunityDatabase:
    """Minimal async facade over a single SQLite connection."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None

    # ── lifecycle ─────────────────────────────────────────────
    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=30, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Community database is not initialized.")
        return self._connection

    async def initialize(self) -> None:
        def create() -> None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = self._open()
            with self._connection as connection:
                connection.executescript(SCHEMA)

        async with self._lock:
            if self._connection is None:
                await asyncio.to_thread(create)
                logger.info("Community database ready at %s", self.database_path)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    # ── queries ───────────────────────────────────────────────
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        def run() -> None:
            with self._require() as connection:
                connection.execute(sql, params)

        async with self._lock:
            await asyncio.to_thread(run)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        payload = list(rows)
        if not payload:
            return

        def run() -> None:
            with self._require() as connection:
                connection.executemany(sql, payload)

        async with self._lock:
            await asyncio.to_thread(run)

    async def execute_write(
        self, sql: str, params: Sequence[Any] = ()
    ) -> WriteResult:
        """
        Runs one write and reports what it touched.

        `rowcount` tells an UPDATE/DELETE that matched nothing apart from one
        that changed a row, and `lastrowid` returns the id of a fresh INSERT.
        """

        def run() -> WriteResult:
            with self._require() as connection:
                cursor = connection.execute(sql, params)
                return WriteResult(cursor.rowcount, cursor.lastrowid)

        async with self._lock:
            return await asyncio.to_thread(run)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def run() -> list[sqlite3.Row]:
            return self._require().execute(sql, params).fetchall()

        async with self._lock:
            return await asyncio.to_thread(run)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        def run() -> sqlite3.Row | None:
            return self._require().execute(sql, params).fetchone()

        async with self._lock:
            return await asyncio.to_thread(run)

    # ── settings helpers ──────────────────────────────────────
    async def get_setting(self, guild_id: int, key: str) -> str | None:
        row = await self.fetchone(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        return row["value"] if row else None

    async def get_settings(
        self, guild_id: int, keys: Sequence[str]
    ) -> dict[str, str | None]:
        """
        Reads several settings in one query.

        Interaction handlers only get three seconds to reply, so anything on
        that path fetches its settings together rather than one at a time.
        """
        if not keys:
            return {}
        placeholders = ", ".join("?" for _ in keys)
        rows = await self.fetchall(
            f"SELECT key, value FROM guild_settings "
            f"WHERE guild_id = ? AND key IN ({placeholders})",
            (guild_id, *keys),
        )
        stored = {row["key"]: row["value"] for row in rows}
        return {key: stored.get(key) for key in keys}

    async def set_setting(self, guild_id: int, key: str, value: str | None) -> None:
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value
            """,
            (guild_id, key, value),
        )


_shared_database: CommunityDatabase | None = None


def get_database(database_path: Path | None = None) -> CommunityDatabase:
    """
    Returns the process-wide community database.

    Both the onboarding and reporting cogs share one instance so a single
    connection (and therefore a single write lock) is used.
    """
    global _shared_database
    if _shared_database is None:
        if database_path is None:
            raise RuntimeError("The community database has not been configured yet.")
        _shared_database = CommunityDatabase(database_path)
    return _shared_database
