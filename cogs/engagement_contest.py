"""
Engagement Contest tracking and leaderboard generation for SafeRest.
Created by Aljay Leodones for SafeRest.

The contest runs in *sessions*. A moderator starts a fresh contest from the Mod
Panel, which fixes the starting point of the count; everything logged from that
instant on belongs to that contest. Ending a contest freezes its final standings
into an archive, so past contests can always be re-opened and viewed.

Scoring
    Genuine message      2 pts
    Reaction             1 pt
    TikTok video        10 pts

Raw activity is stored unscored. Points are worked out at report time by
``build_standings`` replaying every event in chronological order, so the
leaderboard is fully reproducible and the spam rules can be re-tuned without
ever needing to re-scan Discord.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Iterable

import discord
from discord.ext import commands

from config import (
    ACTIVITY_DATABASE_PATH,
    COLOR_ACCENT,
    COMMUNITY_DATABASE_PATH,
    CONTEST_CHANNEL_IDS,
    CONTEST_CROSS_REPEAT_MINUTES,
    CONTEST_EXCLUDED_ROLE_IDS,
    CONTEST_HISTORY_LIMIT,
    CONTEST_MESSAGE_COOLDOWN_SECONDS,
    CONTEST_MIN_MESSAGE_WORDS,
    CONTEST_POINTS_MESSAGE,
    CONTEST_POINTS_REACTION,
    CONTEST_POINTS_TIKTOK,
    CONTEST_REPORT_CHANNEL_ID,
    CONTEST_SELF_REPEAT_HOURS,
    EMBED_FOOTER_TEXT,
)
from core.database import get_database
from utils import embed_builder

logger = logging.getLogger("saferest_bot.engagement_contest")

# Regex to detect TikTok video URLs
TIKTOK_URL_PATTERN = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+", re.IGNORECASE
)

# Everything stripped out before a message is judged "genuine". What is left
# over is the member's own words.
URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")
MENTION_PATTERN = re.compile(r"<[@#][!&]?\d+>|@(?:everyone|here)")
EMOJI_PATTERN = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002600-\U000027bf"
    "\U00002190-\U000021ff"
    "\U00002b00-\U00002bff"
    "️‍⃣"
    "]+"
)

SELF_REPEAT_WINDOW = timedelta(hours=CONTEST_SELF_REPEAT_HOURS)
CROSS_REPEAT_WINDOW = timedelta(minutes=CONTEST_CROSS_REPEAT_MINUTES)
MESSAGE_COOLDOWN = timedelta(seconds=CONTEST_MESSAGE_COOLDOWN_SECONDS)

LEADERBOARD_SIZE = 15


@dataclass(slots=True)
class ContestMemberActivity:
    user_id: int
    display_name: str
    messages: int = 0
    reactions: int = 0
    tiktoks: int = 0
    total_points: int = 0

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "messages": self.messages,
            "reactions": self.reactions,
            "tiktoks": self.tiktoks,
            "total_points": self.total_points,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ContestMemberActivity":
        return cls(
            user_id=int(payload["user_id"]),
            display_name=payload.get("display_name", "Unknown member"),
            messages=int(payload.get("messages", 0)),
            reactions=int(payload.get("reactions", 0)),
            tiktoks=int(payload.get("tiktoks", 0)),
            total_points=int(payload.get("total_points", 0)),
        )


@dataclass(slots=True)
class ContestEvent:
    guild_id: int
    user_id: int
    user_name: str
    channel_id: int
    event_type: str  # 'message', 'reaction', 'tiktok'
    occurred_at: datetime
    event_key: str
    points: int
    content_hash: str = ""
    word_count: int = 0


@dataclass(slots=True)
class ContestScanStats:
    channels_scanned: int = 0
    messages_scanned: int = 0
    reactions_scanned: int = 0
    tiktoks_detected: int = 0
    locations_skipped: int = 0


@dataclass(slots=True)
class ContestFilterStats:
    """Why candidate activity did not turn into points."""

    too_short: int = 0
    repeated: int = 0
    copy_pasted: int = 0
    rate_limited: int = 0
    duplicate_videos: int = 0

    @property
    def total_filtered(self) -> int:
        return (
            self.too_short
            + self.repeated
            + self.copy_pasted
            + self.rate_limited
            + self.duplicate_videos
        )


@dataclass(slots=True)
class ContestSession:
    """One contest run - the window every count is measured against."""

    id: int
    guild_id: int
    name: str
    started_at: datetime
    ended_at: datetime | None
    status: str  # 'active' | 'ended'
    started_by_id: int | None = None
    started_by_name: str = ""
    ended_by_name: str = ""
    snapshot: dict | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def window_end(self) -> datetime:
        return self.ended_at or datetime.now(timezone.utc)

    @property
    def duration(self) -> timedelta:
        return self.window_end - self.started_at

    def describe_duration(self) -> str:
        total_minutes = max(int(self.duration.total_seconds() // 60), 0)
        days, remainder = divmod(total_minutes, 60 * 24)
        hours, minutes = divmod(remainder, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes or not parts:
            parts.append(f"{minutes}m")
        return " ".join(parts)


@dataclass(slots=True)
class StoredEvent:
    """A raw, not-yet-scored row replayed by ``build_standings``."""

    user_id: int
    user_name: str
    event_type: str
    occurred_at: datetime
    event_key: str
    content_hash: str = ""
    word_count: int = 0


# -- Message genuineness helpers -------------------------------------------


def normalize_message_text(content: str | None) -> str:
    """Reduces a message to just the member's own words, lowercased.

    Links, mentions, custom/unicode emoji and punctuation are removed so that
    "OMG!!!" with three heart emoji and a plain "omg" fingerprint identically,
    and neither one passes the word-count gate.
    """
    if not content:
        return ""
    text = content.lower()
    text = URL_PATTERN.sub(" ", text)
    text = CUSTOM_EMOJI_PATTERN.sub(" ", text)
    text = MENTION_PATTERN.sub(" ", text)
    text = EMOJI_PATTERN.sub(" ", text)
    text = re.sub(r"[^\w\s']", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return " ".join(text.split())


def meaningful_word_count(content: str | None) -> int:
    """Number of real words left once links, mentions and emoji are stripped."""
    normalized = normalize_message_text(content)
    return len(normalized.split()) if normalized else 0


def is_genuine_message(content: str | None) -> bool:
    """True when a message carries more than two real words of its own."""
    return meaningful_word_count(content) >= CONTEST_MIN_MESSAGE_WORDS


def content_fingerprint(content: str | None) -> str:
    """Stable hash of the normalized text, used for duplicate detection."""
    return hashlib.sha1(normalize_message_text(content).encode("utf-8")).hexdigest()


def extract_tiktok_links(
    content: str | None, embeds: list[discord.Embed] | None = None
) -> list[str]:
    """Every distinct TikTok URL found in a message body or its embeds."""
    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        if not candidate:
            return
        for match in TIKTOK_URL_PATTERN.findall(candidate):
            cleaned = match.rstrip(").,>\"'").lower()
            if cleaned not in seen:
                seen.add(cleaned)
                found.append(cleaned)

    add(content)
    for embed in embeds or []:
        add(getattr(embed, "url", None))
        add(getattr(embed, "description", None))
    return found


def contains_tiktok_link(
    content: str | None, embeds: list[discord.Embed] | None = None
) -> bool:
    """True if message content or embeds contain a TikTok video link."""
    return bool(extract_tiktok_links(content, embeds))


def video_fingerprint(links: list[str]) -> str:
    """Hash of the TikTok URLs in a post, so the same clip cannot be re-farmed."""
    return hashlib.sha1("|".join(sorted(links)).encode("utf-8")).hexdigest()


def is_contest_channel(
    channel: discord.abc.GuildChannel | discord.Thread | None,
    channel_ids: Iterable[int],
) -> bool:
    """True if the channel is a designated contest channel or a thread inside one."""
    if channel is None:
        return False
    if channel.id in channel_ids:
        return True
    parent_id = getattr(channel, "parent_id", None)
    return bool(parent_id and parent_id in channel_ids)


# Moderators pick the counted channels and the leaderboard channel from the
# Mod Panel (Engagement Contest -> Settings). Saved choices win over config.
CHANNELS_SETTING = "contest_channel_ids"
REPORT_CHANNEL_SETTING = "contest_report_channel_id"


@dataclass(slots=True, frozen=True)
class ContestSettings:
    channel_ids: frozenset[int]
    report_channel_id: int | None


def parse_channel_ids(raw: str | None) -> frozenset[int] | None:
    """``"1,2"`` -> {1, 2}; ``""`` -> empty (cleared); ``None`` -> never saved."""
    if raw is None:
        return None
    return frozenset(int(part) for part in raw.split(",") if part.strip().isdigit())


def is_excluded_member(member: discord.Member | discord.User | None) -> bool:
    """True if the user is a bot or holds any excluded moderator/bot roles."""
    if member is None or getattr(member, "bot", False):
        return True
    roles = getattr(member, "roles", None)
    if roles:
        if any(role.id in CONTEST_EXCLUDED_ROLE_IDS for role in roles):
            return True
    return False


def build_standings(
    events: Iterable[StoredEvent],
) -> tuple[list[ContestMemberActivity], ContestFilterStats]:
    """Replays raw events in order and awards points under the contest rules.

    A message must clear four gates to score: enough real words, not a repeat of
    the member's own recent text, not a copy of somebody else's recent text, and
    not fired off inside the burst cooldown. Reactions always score. A TikTok
    post scores once per clip per member.
    """
    totals: dict[int, ContestMemberActivity] = {}
    stats = ContestFilterStats()

    last_scored_message: dict[int, datetime] = {}
    self_hash_seen: dict[tuple[int, str], datetime] = {}
    any_hash_seen: dict[str, tuple[datetime, int]] = {}
    videos_seen: dict[int, set[str]] = {}

    ordered = sorted(events, key=lambda event: (event.occurred_at, event.event_key))

    for event in ordered:
        activity = totals.get(event.user_id)
        if activity is None:
            activity = ContestMemberActivity(
                user_id=event.user_id, display_name=event.user_name
            )
            totals[event.user_id] = activity
        elif event.user_name:
            activity.display_name = event.user_name

        if event.event_type == "reaction":
            activity.reactions += 1
            activity.total_points += CONTEST_POINTS_REACTION
            continue

        if event.event_type == "tiktok":
            clip_key = event.content_hash or event.event_key
            posted = videos_seen.setdefault(event.user_id, set())
            if clip_key in posted:
                stats.duplicate_videos += 1
                continue
            posted.add(clip_key)
            activity.tiktoks += 1
            activity.total_points += CONTEST_POINTS_TIKTOK
            continue

        # -- Genuine message checks --------------------------------------
        if event.word_count < CONTEST_MIN_MESSAGE_WORDS:
            stats.too_short += 1
            continue

        fingerprint = event.content_hash
        prior_self = self_hash_seen.get((event.user_id, fingerprint))
        prior_any = any_hash_seen.get(fingerprint)

        # Sightings are recorded even when the message is rejected, so that
        # repeated spam keeps sliding its own block window forward.
        self_hash_seen[(event.user_id, fingerprint)] = event.occurred_at
        any_hash_seen[fingerprint] = (event.occurred_at, event.user_id)

        if prior_self is not None and event.occurred_at - prior_self < SELF_REPEAT_WINDOW:
            stats.repeated += 1
            continue

        if (
            prior_any is not None
            and prior_any[1] != event.user_id
            and event.occurred_at - prior_any[0] < CROSS_REPEAT_WINDOW
        ):
            stats.copy_pasted += 1
            continue

        previous = last_scored_message.get(event.user_id)
        if previous is not None and event.occurred_at - previous < MESSAGE_COOLDOWN:
            stats.rate_limited += 1
            continue

        last_scored_message[event.user_id] = event.occurred_at
        activity.messages += 1
        activity.total_points += CONTEST_POINTS_MESSAGE

    ranked = [item for item in totals.values() if item.total_points > 0]
    ranked.sort(
        key=lambda item: (
            item.total_points,
            item.tiktoks,
            item.messages,
            item.reactions,
        ),
        reverse=True,
    )
    return ranked, stats


class ContestStore:
    """SQLite store for contest sessions and the raw events inside them."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=30, check_same_thread=False
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Contest database is not initialized.")
        return self._connection

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        def create_schema() -> None:
            self._connection = self._open_connection()
            with self._connection as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS contest_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        user_name TEXT NOT NULL,
                        channel_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL CHECK (
                            event_type IN ('message', 'reaction', 'tiktok')
                        ),
                        occurred_at TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        points INTEGER NOT NULL,
                        UNIQUE (guild_id, event_key)
                    );

                    CREATE INDEX IF NOT EXISTS contest_events_range_idx
                    ON contest_events (guild_id, occurred_at, user_id);

                    CREATE TABLE IF NOT EXISTS contest_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        status TEXT NOT NULL CHECK (status IN ('active', 'ended')),
                        started_by_id INTEGER,
                        started_by_name TEXT NOT NULL DEFAULT '',
                        ended_by_name TEXT NOT NULL DEFAULT '',
                        snapshot_json TEXT
                    );

                    CREATE INDEX IF NOT EXISTS contest_sessions_guild_idx
                    ON contest_sessions (guild_id, started_at DESC);

                    CREATE UNIQUE INDEX IF NOT EXISTS contest_sessions_single_active_idx
                    ON contest_sessions (guild_id) WHERE status = 'active';
                    """
                )

                # Migration: v1 stored no genuineness metadata on events.
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(contest_events)")
                }
                if "content_hash" not in columns:
                    connection.execute(
                        "ALTER TABLE contest_events ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"
                    )
                if "word_count" not in columns:
                    connection.execute(
                        "ALTER TABLE contest_events ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0"
                    )

        async with self._lock:
            await asyncio.to_thread(create_schema)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    # -- Events ------------------------------------------------------------

    async def record_events(self, events: Iterable[ContestEvent]) -> int:
        rows = [
            (
                event.guild_id,
                event.user_id,
                event.user_name,
                event.channel_id,
                event.event_type,
                event.occurred_at.astimezone(timezone.utc).isoformat(),
                event.event_key,
                event.points,
                event.content_hash,
                event.word_count,
            )
            for event in events
        ]
        if not rows:
            return 0

        def insert() -> int:
            with self._require_connection() as connection:
                changes_before = connection.total_changes
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO contest_events (
                        guild_id, user_id, user_name, channel_id,
                        event_type, occurred_at, event_key, points,
                        content_hash, word_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                return connection.total_changes - changes_before

        async with self._lock:
            return await asyncio.to_thread(insert)

    async def fetch_events(
        self, guild_id: int, start: datetime, end: datetime
    ) -> list[StoredEvent]:
        """Raw, unscored events inside a window, oldest first."""

        def select() -> list[StoredEvent]:
            rows = self._require_connection().execute(
                """
                SELECT user_id, user_name, event_type, occurred_at,
                       event_key, content_hash, word_count
                FROM contest_events
                WHERE guild_id = ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at ASC, id ASC
                """,
                (
                    guild_id,
                    start.astimezone(timezone.utc).isoformat(),
                    end.astimezone(timezone.utc).isoformat(),
                ),
            ).fetchall()
            return [
                StoredEvent(
                    user_id=row[0],
                    user_name=row[1],
                    event_type=row[2],
                    occurred_at=_parse_timestamp(row[3]),
                    event_key=row[4],
                    content_hash=row[5] or "",
                    word_count=row[6] or 0,
                )
                for row in rows
            ]

        async with self._lock:
            return await asyncio.to_thread(select)

    async def aggregate(
        self, guild_id: int, start: datetime, end: datetime
    ) -> list[ContestMemberActivity]:
        """Final, rule-applied standings for a window."""
        events = await self.fetch_events(guild_id, start, end)
        ranked, _ = build_standings(events)
        return ranked

    async def aggregate_with_stats(
        self, guild_id: int, start: datetime, end: datetime
    ) -> tuple[list[ContestMemberActivity], ContestFilterStats]:
        events = await self.fetch_events(guild_id, start, end)
        return build_standings(events)

    # -- Sessions ----------------------------------------------------------

    async def get_active_session(self, guild_id: int) -> ContestSession | None:
        def select() -> ContestSession | None:
            row = self._require_connection().execute(
                f"SELECT {_SESSION_COLUMNS} FROM contest_sessions "
                "WHERE guild_id = ? AND status = 'active' LIMIT 1",
                (guild_id,),
            ).fetchone()
            return _row_to_session(row) if row else None

        async with self._lock:
            return await asyncio.to_thread(select)

    async def get_session(self, session_id: int) -> ContestSession | None:
        def select() -> ContestSession | None:
            row = self._require_connection().execute(
                f"SELECT {_SESSION_COLUMNS} FROM contest_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            return _row_to_session(row) if row else None

        async with self._lock:
            return await asyncio.to_thread(select)

    async def list_sessions(
        self, guild_id: int, *, limit: int = CONTEST_HISTORY_LIMIT
    ) -> list[ContestSession]:
        def select() -> list[ContestSession]:
            rows = self._require_connection().execute(
                f"SELECT {_SESSION_COLUMNS} FROM contest_sessions "
                "WHERE guild_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
                (guild_id, limit),
            ).fetchall()
            return [_row_to_session(row) for row in rows]

        async with self._lock:
            return await asyncio.to_thread(select)

    async def start_session(
        self,
        *,
        guild_id: int,
        name: str,
        started_at: datetime,
        started_by_id: int | None,
        started_by_name: str,
    ) -> ContestSession:
        """Opens a new contest. Any contest still running is closed first."""

        def insert() -> int:
            with self._require_connection() as connection:
                connection.execute(
                    "UPDATE contest_sessions SET status = 'ended', ended_at = ?, "
                    "ended_by_name = ? WHERE guild_id = ? AND status = 'active'",
                    (
                        started_at.astimezone(timezone.utc).isoformat(),
                        "Superseded by a new contest",
                        guild_id,
                    ),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO contest_sessions (
                        guild_id, name, started_at, ended_at, status,
                        started_by_id, started_by_name, ended_by_name, snapshot_json
                    ) VALUES (?, ?, ?, NULL, 'active', ?, ?, '', NULL)
                    """,
                    (
                        guild_id,
                        name,
                        started_at.astimezone(timezone.utc).isoformat(),
                        started_by_id,
                        started_by_name,
                    ),
                )
                return int(cursor.lastrowid)

        async with self._lock:
            session_id = await asyncio.to_thread(insert)

        session = await self.get_session(session_id)
        if session is None:  # pragma: no cover - the row was just written
            raise RuntimeError("The new contest could not be created.")
        return session

    async def end_session(
        self,
        session_id: int,
        *,
        ended_at: datetime,
        ended_by_name: str,
        snapshot: dict,
    ) -> ContestSession | None:
        def update() -> None:
            with self._require_connection() as connection:
                connection.execute(
                    "UPDATE contest_sessions SET status = 'ended', ended_at = ?, "
                    "ended_by_name = ?, snapshot_json = ? WHERE id = ?",
                    (
                        ended_at.astimezone(timezone.utc).isoformat(),
                        ended_by_name,
                        json.dumps(snapshot),
                        session_id,
                    ),
                )

        async with self._lock:
            await asyncio.to_thread(update)
        return await self.get_session(session_id)

    async def save_snapshot(self, session_id: int, snapshot: dict) -> None:
        """Caches the latest standings on a session without ending it."""

        def update() -> None:
            with self._require_connection() as connection:
                connection.execute(
                    "UPDATE contest_sessions SET snapshot_json = ? WHERE id = ?",
                    (json.dumps(snapshot), session_id),
                )

        async with self._lock:
            await asyncio.to_thread(update)

    async def reset_guild(self, guild_id: int) -> tuple[int, int]:
        """Wipes every event and contest for a guild. Returns (events, contests)."""

        def purge() -> tuple[int, int]:
            with self._require_connection() as connection:
                events = connection.execute(
                    "SELECT COUNT(*) FROM contest_events WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()[0]
                sessions = connection.execute(
                    "SELECT COUNT(*) FROM contest_sessions WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM contest_events WHERE guild_id = ?", (guild_id,)
                )
                connection.execute(
                    "DELETE FROM contest_sessions WHERE guild_id = ?", (guild_id,)
                )
                return int(events), int(sessions)

        async with self._lock:
            return await asyncio.to_thread(purge)


_SESSION_COLUMNS = (
    "id, guild_id, name, started_at, ended_at, status, "
    "started_by_id, started_by_name, ended_by_name, snapshot_json"
)


def _parse_timestamp(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_to_session(row) -> ContestSession:
    snapshot = None
    if row[9]:
        try:
            snapshot = json.loads(row[9])
        except (TypeError, ValueError):
            logger.warning("Contest %s has an unreadable standings snapshot", row[0])
    return ContestSession(
        id=row[0],
        guild_id=row[1],
        name=row[2],
        started_at=_parse_timestamp(row[3]),
        ended_at=_parse_timestamp(row[4]) if row[4] else None,
        status=row[5],
        started_by_id=row[6],
        started_by_name=row[7] or "",
        ended_by_name=row[8] or "",
        snapshot=snapshot,
    )


class EngagementContestCog(commands.Cog, name="EngagementContestCog"):
    """Runs the Engagement Contest: sessions, scoring and the Top 15 Leaderboard."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = ContestStore(ACTIVITY_DATABASE_PATH)
        self.db = get_database(COMMUNITY_DATABASE_PATH)
        # Every message in the server checks the counted channels, so they are
        # kept in memory and only re-read after a moderator changes them.
        self._settings: dict[int, ContestSettings] = {}

    async def cog_load(self) -> None:
        await self.store.initialize()
        await self.db.initialize()
        logger.info("Engagement contest store ready at %s", ACTIVITY_DATABASE_PATH)

    # -- Settings ------------------------------------------------------------

    async def settings(self, guild_id: int) -> ContestSettings:
        cached = self._settings.get(guild_id)
        if cached is not None:
            return cached
        stored = await self.db.get_settings(
            guild_id, [CHANNELS_SETTING, REPORT_CHANNEL_SETTING]
        )
        channel_ids = parse_channel_ids(stored.get(CHANNELS_SETTING))
        raw_report = stored.get(REPORT_CHANNEL_SETTING)
        settings = ContestSettings(
            channel_ids=(
                channel_ids if channel_ids is not None else frozenset(CONTEST_CHANNEL_IDS)
            ),
            report_channel_id=(
                int(raw_report)
                if raw_report and raw_report.isdigit()
                else CONTEST_REPORT_CHANNEL_ID
            ),
        )
        self._settings[guild_id] = settings
        return settings

    async def set_channels(self, guild_id: int, channel_ids: Iterable[int]) -> None:
        value = ",".join(str(cid) for cid in sorted(set(channel_ids)))
        await self.db.set_setting(guild_id, CHANNELS_SETTING, value)
        self._settings.pop(guild_id, None)

    async def set_report_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self.db.set_setting(
            guild_id, REPORT_CHANNEL_SETTING, str(channel_id) if channel_id else None
        )
        self._settings.pop(guild_id, None)

    async def cog_unload(self) -> None:
        await self.store.close()

    # -- Contest session management ---------------------------------------

    async def get_active_contest(self, guild_id: int) -> ContestSession | None:
        return await self.store.get_active_session(guild_id)

    async def list_contests(self, guild_id: int) -> list[ContestSession]:
        return await self.store.list_sessions(guild_id)

    async def get_contest(self, session_id: int) -> ContestSession | None:
        return await self.store.get_session(session_id)

    async def start_contest(
        self,
        *,
        guild: discord.Guild,
        name: str,
        started_by: discord.Member | discord.User,
    ) -> ContestSession:
        """Opens a fresh contest whose count starts at this exact moment."""
        session = await self.store.start_session(
            guild_id=guild.id,
            name=name,
            started_at=datetime.now(timezone.utc),
            started_by_id=started_by.id,
            started_by_name=getattr(started_by, "display_name", str(started_by)),
        )
        logger.info(
            "Contest %s (%r) started in guild %s by %s",
            session.id,
            session.name,
            guild.id,
            session.started_by_name,
        )
        return session

    async def end_contest(
        self,
        *,
        guild: discord.Guild,
        session: ContestSession,
        ended_by: discord.Member | discord.User,
    ) -> tuple[ContestSession, list[ContestMemberActivity]]:
        """Closes a contest and freezes its final standings into the archive."""
        ended_at = datetime.now(timezone.utc)
        standings, filter_stats, scan_stats = await self.collect_standings(
            guild=guild, start=session.started_at, end=ended_at
        )
        snapshot = build_snapshot(
            standings=standings,
            filter_stats=filter_stats,
            scan_stats=scan_stats,
            channel_ids=(await self.settings(guild.id)).channel_ids,
            period_label=describe_session_period(session, ended_at=ended_at),
            generated_at=ended_at,
        )
        updated = await self.store.end_session(
            session.id,
            ended_at=ended_at,
            ended_by_name=getattr(ended_by, "display_name", str(ended_by)),
            snapshot=snapshot,
        )
        logger.info("Contest %s ended in guild %s", session.id, guild.id)
        return updated or session, standings

    async def reset_contest_data(self, guild: discord.Guild) -> tuple[int, int]:
        """Wipes every recorded count and contest for the guild."""
        events, sessions = await self.store.reset_guild(guild.id)
        logger.warning(
            "Contest data reset for guild %s (%s events, %s contests removed)",
            guild.id,
            events,
            sessions,
        )
        return events, sessions

    # -- Live tracking -----------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        settings = await self.settings(message.guild.id)
        if not is_contest_channel(message.channel, settings.channel_ids):
            return
        if is_excluded_member(message.author):
            return

        event = build_message_event(
            guild_id=message.guild.id,
            user_id=message.author.id,
            user_name=message.author.display_name,
            channel_id=message.channel.id,
            message_id=message.id,
            content=message.content,
            embeds=message.embeds,
            occurred_at=message.created_at,
        )
        await self.store.record_events([event])

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or (
            self.bot.user is not None and payload.user_id == self.bot.user.id
        ):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        settings = await self.settings(guild.id)
        if not is_contest_channel(channel, settings.channel_ids):
            return

        member = payload.member
        if member is None:
            member = guild.get_member(payload.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return

        if is_excluded_member(member):
            return

        await self.store.record_events(
            [
                ContestEvent(
                    guild_id=payload.guild_id,
                    user_id=payload.user_id,
                    user_name=member.display_name,
                    channel_id=payload.channel_id,
                    event_type="reaction",
                    occurred_at=datetime.now(timezone.utc),
                    event_key=(
                        f"contest_reaction:{payload.message_id}:"
                        f"{payload.user_id}:{payload.emoji}"
                    ),
                    points=CONTEST_POINTS_REACTION,
                )
            ]
        )

    # -- History backfill --------------------------------------------------

    async def backfill_contest_history(
        self, guild: discord.Guild, start: datetime, end: datetime
    ) -> ContestScanStats:
        """Backfills activity from the contest channels and their threads."""
        stats = ContestScanStats()
        member_cache: dict[int, discord.Member | None] = {}

        async def get_member_cached(user_id: int) -> discord.Member | None:
            if user_id in member_cache:
                return member_cache[user_id]
            found = guild.get_member(user_id)
            if found is None:
                try:
                    found = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    found = None
            member_cache[user_id] = found
            return found

        async def scan_channel_messages(
            location: discord.TextChannel | discord.Thread,
        ) -> None:
            pending: list[ContestEvent] = []
            try:
                async for message in location.history(
                    limit=None, after=start, before=end, oldest_first=True
                ):
                    stats.messages_scanned += 1
                    author = await get_member_cached(message.author.id)
                    if author is not None and not is_excluded_member(author):
                        event = build_message_event(
                            guild_id=guild.id,
                            user_id=author.id,
                            user_name=author.display_name,
                            channel_id=location.id,
                            message_id=message.id,
                            content=message.content,
                            embeds=message.embeds,
                            occurred_at=message.created_at,
                        )
                        if event.event_type == "tiktok":
                            stats.tiktoks_detected += 1
                        pending.append(event)

                    for reaction in message.reactions:
                        try:
                            async for user in reaction.users(limit=None):
                                if user.bot:
                                    continue
                                reactor = await get_member_cached(user.id)
                                if reactor is None or is_excluded_member(reactor):
                                    continue
                                stats.reactions_scanned += 1
                                pending.append(
                                    ContestEvent(
                                        guild_id=guild.id,
                                        user_id=reactor.id,
                                        user_name=reactor.display_name,
                                        channel_id=location.id,
                                        event_type="reaction",
                                        occurred_at=message.created_at,
                                        event_key=(
                                            f"contest_reaction:{message.id}:"
                                            f"{user.id}:{reaction.emoji}"
                                        ),
                                        points=CONTEST_POINTS_REACTION,
                                    )
                                )
                        except (
                            discord.Forbidden,
                            discord.NotFound,
                            discord.HTTPException,
                        ):
                            pass

                    if len(pending) >= 500:
                        await self.store.record_events(pending)
                        pending.clear()

            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                stats.locations_skipped += 1
                logger.warning(
                    "Could not read contest message history in %s", location.id
                )
            finally:
                if pending:
                    await self.store.record_events(pending)

        settings = await self.settings(guild.id)
        for channel_id in sorted(settings.channel_ids):
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    stats.locations_skipped += 1
                    continue

            if isinstance(channel, discord.TextChannel):
                await scan_channel_messages(channel)
                stats.channels_scanned += 1

                # Also scan active and archived threads belonging to this channel
                try:
                    for thread in channel.threads:
                        await scan_channel_messages(thread)
                except Exception:  # noqa: BLE001 - a bad thread must not stop the scan
                    pass

                try:
                    async for archived in channel.archived_threads(
                        limit=None, before=end
                    ):
                        if (
                            archived.archive_timestamp
                            and archived.archive_timestamp < start
                        ):
                            break
                        await scan_channel_messages(archived)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass

            elif isinstance(channel, discord.Thread):
                await scan_channel_messages(channel)
                stats.channels_scanned += 1

        logger.info(
            "Contest history scan for guild %s inspected %s messages across %s channels/threads",
            guild.id,
            stats.messages_scanned,
            stats.channels_scanned,
        )
        return stats

    # -- Standings & reporting --------------------------------------------

    async def collect_standings(
        self,
        *,
        guild: discord.Guild,
        start: datetime,
        end: datetime,
        scan: bool = True,
    ) -> tuple[list[ContestMemberActivity], ContestFilterStats, ContestScanStats]:
        """Backfills, scores and filters out anyone who is no longer eligible."""
        if scan:
            scan_stats = await self.backfill_contest_history(guild, start, end)
        else:
            settings = await self.settings(guild.id)
            scan_stats = ContestScanStats(channels_scanned=len(settings.channel_ids))

        ranked, filter_stats = await self.store.aggregate_with_stats(
            guild.id, start, end
        )

        qualified: list[ContestMemberActivity] = []
        for activity in ranked:
            member = guild.get_member(activity.user_id)
            if member is not None:
                if is_excluded_member(member):
                    continue
                activity.display_name = member.display_name
            qualified.append(activity)

        return qualified, filter_stats, scan_stats

    async def resolve_report_channel(
        self, guild: discord.Guild
    ) -> discord.TextChannel | discord.Thread:
        report_channel_id = (await self.settings(guild.id)).report_channel_id
        if report_channel_id is None:
            raise RuntimeError(
                "No leaderboard channel is set. Pick one in Engagement Contest → Settings."
            )

        channel = guild.get_channel(report_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(report_channel_id)
            except discord.NotFound as exc:
                raise RuntimeError(
                    f"Leaderboard channel {report_channel_id} was not found in this "
                    "server. Pick another in Engagement Contest → Settings."
                ) from exc
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError(
                "The configured contest report channel is not a text channel or thread."
            )
        return channel

    async def publish_contest_leaderboard(
        self,
        *,
        guild: discord.Guild,
        start: datetime,
        end: datetime,
        period_label: str,
        requested_by: discord.Member | discord.User,
        session: ContestSession | None = None,
        is_final: bool = False,
    ) -> tuple[
        discord.abc.Messageable,
        list[ContestMemberActivity],
        ContestScanStats,
        ContestFilterStats,
    ]:
        """Scans, scores and posts the Top 15 Leaderboard to the report channel."""
        channel = await self.resolve_report_channel(guild)

        standings, filter_stats, scan_stats = await self.collect_standings(
            guild=guild, start=start, end=end
        )
        channel_ids = (await self.settings(guild.id)).channel_ids

        if session is not None:
            await self.store.save_snapshot(
                session.id,
                build_snapshot(
                    standings=standings,
                    filter_stats=filter_stats,
                    scan_stats=scan_stats,
                    channel_ids=channel_ids,
                    period_label=period_label,
                    generated_at=datetime.now(timezone.utc),
                ),
            )

        embed = render_leaderboard_embed(
            standings=standings,
            filter_stats=filter_stats,
            channels_inspected=scan_stats.channels_scanned,
            channel_ids=channel_ids,
            period_label=period_label,
            contest_name=session.name if session else None,
            requested_by=requested_by,
            is_final=is_final,
        )

        headline = (
            "🏁 **SafeRest Engagement Contest — FINAL RESULTS**"
            if is_final
            else "🏆 **SafeRest Engagement Contest Leaderboard**"
        )
        await channel.send(
            content=(
                f"{headline}\n"
                f"Period: **{period_label}** • Generated by {requested_by.mention}"
            ),
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )

        logger.info(
            "Published contest leaderboard for guild %s to channel %s covering %s to %s",
            guild.id,
            channel.id,
            start,
            end,
        )
        return channel, standings[:LEADERBOARD_SIZE], scan_stats, filter_stats


# -- Event construction -----------------------------------------------------


def build_message_event(
    *,
    guild_id: int,
    user_id: int,
    user_name: str,
    channel_id: int,
    message_id: int,
    content: str | None,
    embeds: list[discord.Embed] | None,
    occurred_at: datetime,
) -> ContestEvent:
    """Turns a Discord message into the raw event row the scorer replays later.

    Nothing is judged here. The word count and content fingerprint are captured
    so ``build_standings`` can apply the genuine-message rules at report time.
    """
    tiktok_links = extract_tiktok_links(content, embeds)
    if tiktok_links:
        return ContestEvent(
            guild_id=guild_id,
            user_id=user_id,
            user_name=user_name,
            channel_id=channel_id,
            event_type="tiktok",
            occurred_at=occurred_at,
            event_key=f"contest_msg:{message_id}",
            points=CONTEST_POINTS_TIKTOK,
            content_hash=video_fingerprint(tiktok_links),
            word_count=meaningful_word_count(content),
        )

    return ContestEvent(
        guild_id=guild_id,
        user_id=user_id,
        user_name=user_name,
        channel_id=channel_id,
        event_type="message",
        occurred_at=occurred_at,
        event_key=f"contest_msg:{message_id}",
        points=CONTEST_POINTS_MESSAGE,
        content_hash=content_fingerprint(content),
        word_count=meaningful_word_count(content),
    )


# -- Report rendering -------------------------------------------------------


def describe_session_period(
    session: ContestSession, *, ended_at: datetime | None = None
) -> str:
    """`Spring Giveaway • 14/09 03:12 PM -> now (2d 4h)` style period label."""
    tz = embed_builder.display_timezone()
    start_local = session.started_at.astimezone(tz).strftime("%d %b %Y, %I:%M %p")
    finish = ended_at or session.ended_at
    if finish is None:
        return f"{start_local} → now • running {session.describe_duration()}"
    end_local = finish.astimezone(tz).strftime("%d %b %Y, %I:%M %p")
    span = finish - session.started_at
    minutes = max(int(span.total_seconds() // 60), 0)
    days, remainder = divmod(minutes, 60 * 24)
    hours, mins = divmod(remainder, 60)
    parts = [f"{days}d" for _ in (1,) if days]
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return f"{start_local} → {end_local} • {' '.join(parts)}"


def build_snapshot(
    *,
    standings: list[ContestMemberActivity],
    filter_stats: ContestFilterStats,
    scan_stats: ContestScanStats,
    period_label: str,
    generated_at: datetime,
    channel_ids: Iterable[int] = (),
) -> dict:
    """Freezes a set of standings so a past contest can be re-rendered."""
    return {
        "version": 2,
        "period_label": period_label,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "channels_inspected": scan_stats.channels_scanned,
        "channel_ids": sorted(channel_ids),
        "filtered": {
            "too_short": filter_stats.too_short,
            "repeated": filter_stats.repeated,
            "copy_pasted": filter_stats.copy_pasted,
            "rate_limited": filter_stats.rate_limited,
            "duplicate_videos": filter_stats.duplicate_videos,
        },
        "standings": [activity.as_dict() for activity in standings],
    }


def snapshot_to_standings(
    snapshot: dict | None,
) -> tuple[list[ContestMemberActivity], ContestFilterStats, int]:
    """Rebuilds standings, filter counts and channel count from a snapshot."""
    if not snapshot:
        return [], ContestFilterStats(), 0
    standings = [
        ContestMemberActivity.from_dict(row) for row in snapshot.get("standings", [])
    ]
    raw_filtered = snapshot.get("filtered", {})
    filter_stats = ContestFilterStats(
        too_short=int(raw_filtered.get("too_short", 0)),
        repeated=int(raw_filtered.get("repeated", 0)),
        copy_pasted=int(raw_filtered.get("copy_pasted", 0)),
        rate_limited=int(raw_filtered.get("rate_limited", 0)),
        duplicate_videos=int(raw_filtered.get("duplicate_videos", 0)),
    )
    return standings, filter_stats, int(snapshot.get("channels_inspected", 0))


def snapshot_channel_ids(snapshot: dict | None) -> list[int] | None:
    """The channels a frozen contest counted, or None for older snapshots."""
    if not snapshot or "channel_ids" not in snapshot:
        return None
    return [int(cid) for cid in snapshot["channel_ids"]]


def render_leaderboard_embed(
    *,
    standings: list[ContestMemberActivity],
    filter_stats: ContestFilterStats,
    channels_inspected: int,
    period_label: str,
    contest_name: str | None,
    channel_ids: Iterable[int] = (),
    requested_by: discord.Member | discord.User | None = None,
    is_final: bool = False,
) -> discord.Embed:
    """Builds the Top 15 Leaderboard embed used for live, final and past reports."""
    top_rows = standings[:LEADERBOARD_SIZE]
    total_participants = len(standings)
    total_points_sum = sum(item.total_points for item in standings)
    total_messages_sum = sum(item.messages for item in standings)
    total_tiktoks_sum = sum(item.tiktoks for item in standings)
    total_reactions_sum = sum(item.reactions for item in standings)

    heading = "🏁  Final Results" if is_final else "🏆  Engagement Contest Leaderboard"
    title = f"{heading} — SafeRest"
    if contest_name:
        title = f"{heading} — {contest_name}"

    embed = discord.Embed(
        title=title,
        description=(
            f"{embed_builder.SEPARATOR}\n"
            f"Official contest standings for **{period_label}**.\n\n"
            f"**Scoring System:**\n"
            f"▸ 💬 Genuine Messages: **{CONTEST_POINTS_MESSAGE} pts** "
            f"(more than {CONTEST_MIN_MESSAGE_WORDS - 1} words, no spam or copy-paste)\n"
            f"▸ 🎬 TikTok Videos: **{CONTEST_POINTS_TIKTOK} pts per post**\n"
            f"▸ ❤️ Reactions: **{CONTEST_POINTS_REACTION} pt**\n"
            f"▸ 🛡️ Excluded: Moderators and Bots\n"
            f"{embed_builder.SEPARATOR}"
        ),
        color=COLOR_ACCENT,
        timestamp=datetime.now(timezone.utc),
    )

    channels_mentions = " ".join(f"<#{cid}>" for cid in sorted(channel_ids))
    embed.add_field(
        name=embed_builder.section("Target Channels"),
        value=channels_mentions or "Designated contest channels",
        inline=False,
    )

    rank_lines: list[str] = []
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    for rank, item in enumerate(top_rows, start=1):
        badge = medals.get(rank, "🎖️" if rank <= 10 else "🎗️")
        line = (
            f"{badge} **#{rank}** <@{item.user_id}> — **`{item.total_points:,}`** pts\n"
            f"   └ 💬 {item.messages:,} msgs "
            f"({item.messages * CONTEST_POINTS_MESSAGE:,} pts) • "
            f"🎬 {item.tiktoks:,} TikToks "
            f"({item.tiktoks * CONTEST_POINTS_TIKTOK:,} pts) • "
            f"❤️ {item.reactions:,} reacts "
            f"({item.reactions * CONTEST_POINTS_REACTION:,} pts)"
        )
        rank_lines.append(line)

    if not rank_lines:
        rank_lines = [
            "No qualifying member engagement was recorded in the select channels "
            "for this period."
        ]

    chunk_size = 8
    if len(rank_lines) <= chunk_size:
        embed.add_field(
            name=embed_builder.section(f"Top {len(top_rows)} Leaderboard"),
            value="\n".join(rank_lines),
            inline=False,
        )
    else:
        embed.add_field(
            name=embed_builder.section(
                f"Top Leaderboard (1-{min(chunk_size, len(rank_lines))})"
            ),
            value="\n".join(rank_lines[:chunk_size]),
            inline=False,
        )
        embed.add_field(
            name=embed_builder.section(
                f"Top Leaderboard ({chunk_size + 1}-{len(rank_lines)})"
            ),
            value="\n".join(rank_lines[chunk_size:]),
            inline=False,
        )

    embed.add_field(
        name=embed_builder.section("Contest Summary"),
        value=(
            f"▸ **Total Points Awarded:** `{total_points_sum:,}`\n"
            f"▸ **Qualifying Participants:** `{total_participants:,}`\n"
            f"▸ **Messages Counted:** `{total_messages_sum:,}`\n"
            f"▸ **TikTok Video Posts:** `{total_tiktoks_sum:,}`\n"
            f"▸ **Reactions Counted:** `{total_reactions_sum:,}`\n"
            f"▸ **Channels Inspected:** `{channels_inspected:,}`\n"
            f"▸ **Spam / Duplicates Filtered:** `{filter_stats.total_filtered:,}`"
        ),
        inline=False,
    )

    footer = EMBED_FOOTER_TEXT
    if requested_by is not None:
        footer = f"{footer} • Requested by {requested_by.display_name}"
    embed.set_footer(text=footer)
    return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EngagementContestCog(bot))
