"""Persistent member engagement tracking and formal report generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import logging
from pathlib import Path
import sqlite3
from typing import Iterable

import discord
from discord.ext import commands

from config import (
    ACTIVITY_DATABASE_PATH,
    BRAND_COLOR,
    BRAND_NAME,
    EMBED_FOOTER_TEXT,
    REPORT_CHANNEL_ID,
)

logger = logging.getLogger("saferest_bot.engagement")


@dataclass(slots=True)
class MemberActivity:
    user_id: int
    display_name: str
    messages: int = 0
    reactions: int = 0
    threads: int = 0

    @property
    def total(self) -> int:
        """One activity point is awarded for each tracked action."""
        return self.messages + self.reactions + self.threads


@dataclass(slots=True)
class ActivityEvent:
    guild_id: int
    user_id: int
    user_name: str
    event_type: str
    occurred_at: datetime
    event_key: str
    channel_id: int | None


@dataclass(slots=True)
class HistorySyncStats:
    channels_scanned: int = 0
    threads_scanned: int = 0
    messages_scanned: int = 0
    reactions_scanned: int = 0
    locations_skipped: int = 0


class EngagementStore:
    """Small asynchronous wrapper around the bot's SQLite event ledger."""

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
            raise RuntimeError("Engagement database is not initialized.")
        return self._connection

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        def create_schema() -> None:
            self._connection = self._open_connection()
            with self._connection as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS activity_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        user_name TEXT NOT NULL,
                        event_type TEXT NOT NULL CHECK (
                            event_type IN ('message', 'reaction', 'thread')
                        ),
                        occurred_at TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        channel_id INTEGER,
                        UNIQUE (guild_id, event_key)
                    );

                    CREATE INDEX IF NOT EXISTS activity_events_range_idx
                    ON activity_events (guild_id, occurred_at, user_id);

                    CREATE TABLE IF NOT EXISTS engagement_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO engagement_metadata (key, value)
                    VALUES ('tracking_started_at', ?)
                    """,
                    (datetime.now(timezone.utc).isoformat(),),
                )

        async with self._lock:
            await asyncio.to_thread(create_schema)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    async def record_event(
        self,
        *,
        guild_id: int,
        user_id: int,
        user_name: str,
        event_type: str,
        occurred_at: datetime,
        event_key: str,
        channel_id: int | None,
    ) -> None:
        await self.record_events(
            [
                ActivityEvent(
                    guild_id=guild_id,
                    user_id=user_id,
                    user_name=user_name,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    event_key=event_key,
                    channel_id=channel_id,
                )
            ]
        )

    async def record_events(self, events: Iterable[ActivityEvent]) -> int:
        event_rows = [
            (
                event.guild_id,
                event.user_id,
                event.user_name,
                event.event_type,
                event.occurred_at.astimezone(timezone.utc).isoformat(),
                event.event_key,
                event.channel_id,
            )
            for event in events
        ]
        if not event_rows:
            return 0

        def insert() -> int:
            with self._require_connection() as connection:
                changes_before = connection.total_changes
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO activity_events (
                        guild_id, user_id, user_name, event_type,
                        occurred_at, event_key, channel_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    event_rows,
                )
                return connection.total_changes - changes_before

        async with self._lock:
            return await asyncio.to_thread(insert)

    async def aggregate(
        self, guild_id: int, start: datetime, end: datetime
    ) -> list[MemberActivity]:
        def select() -> list[MemberActivity]:
            rows = self._require_connection().execute(
                """
                SELECT
                    user_id,
                    MAX(user_name),
                    SUM(CASE WHEN event_type = 'message' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN event_type = 'reaction' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN event_type = 'thread' THEN 1 ELSE 0 END)
                FROM activity_events
                WHERE guild_id = ? AND occurred_at >= ? AND occurred_at < ?
                GROUP BY user_id
                """,
                (
                    guild_id,
                    start.astimezone(timezone.utc).isoformat(),
                    end.astimezone(timezone.utc).isoformat(),
                ),
            ).fetchall()
            return [MemberActivity(*row) for row in rows]

        async with self._lock:
            return await asyncio.to_thread(select)

    async def tracking_started_at(self) -> datetime:
        def select() -> str:
            row = self._require_connection().execute(
                "SELECT value FROM engagement_metadata WHERE key = 'tracking_started_at'"
            ).fetchone()
            return row[0]

        async with self._lock:
            value = await asyncio.to_thread(select)
        return datetime.fromisoformat(value)


def _reaction_event_key(message_id: int, user_id: int, emoji: object) -> str:
    return f"reaction:{message_id}:{user_id}:{emoji}"


def _safe_name(name: str) -> str:
    return " ".join(name.replace("\t", " ").splitlines()).strip() or "Unknown Member"


def _merge_current_members(
    guild: discord.Guild, activity: Iterable[MemberActivity]
) -> list[MemberActivity]:
    by_id = {row.user_id: row for row in activity}
    for member in guild.members:
        if member.bot:
            continue
        if member.id in by_id:
            by_id[member.id].display_name = member.display_name
        else:
            by_id[member.id] = MemberActivity(member.id, member.display_name)

    rows = list(by_id.values())
    rows.sort(
        key=lambda row: (
            -row.total,
            -row.messages,
            -row.reactions,
            -row.threads,
            row.display_name.casefold(),
        )
    )
    return rows


def _build_text_report(
    *,
    guild: discord.Guild,
    rows: list[MemberActivity],
    start: datetime,
    end: datetime,
    period_label: str,
    requested_by: discord.abc.User,
    tracking_started: datetime,
    ranking_limit: int,
    sync_stats: HistorySyncStats,
) -> str:
    visible_rows = rows[:ranking_limit]
    total_messages = sum(row.messages for row in rows)
    total_reactions = sum(row.reactions for row in rows)
    total_threads = sum(row.threads for row in rows)
    generated_at = datetime.now(timezone.utc)

    lines = [
        f"{BRAND_NAME.upper()} — MEMBER ACTIVITY & ENGAGEMENT REPORT",
        "=" * 72,
        f"Server: {guild.name} ({guild.id})",
        f"Report period: {period_label}",
        f"From (UTC): {start.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}",
        f"To (UTC, exclusive): {end.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}",
        f"Generated (UTC): {generated_at:%Y-%m-%d %H:%M:%S}",
        f"Requested by: {_safe_name(requested_by.display_name)} ({requested_by.id})",
        f"Live activity tracking began (UTC): {tracking_started.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}",
        "",
        "EXECUTIVE SUMMARY",
        "-" * 72,
        f"Members evaluated: {len(rows):,}",
        f"Ranked members included: {len(visible_rows):,} (policy cap: {ranking_limit:,})",
        f"Messages sent: {total_messages:,}",
        f"Reactions added: {total_reactions:,}",
        f"Threads created: {total_threads:,}",
        f"Total activity points: {total_messages + total_reactions + total_threads:,}",
        "Scoring: 1 point per message, reaction added, or thread created.",
        "",
        "HISTORY SCAN",
        "-" * 72,
        f"Text channels scanned: {sync_stats.channels_scanned:,}",
        f"Threads scanned: {sync_stats.threads_scanned:,}",
        f"Messages inspected: {sync_stats.messages_scanned:,}",
        f"Visible reactions inspected: {sync_stats.reactions_scanned:,}",
        f"Unreadable locations skipped: {sync_stats.locations_skipped:,}",
        "",
        "MEMBER RANKING",
        "-" * 72,
        "Rank\tMember\tUser ID\tMessages\tReactions\tThreads\tActivity Points",
    ]

    for rank, row in enumerate(visible_rows, start=1):
        lines.append(
            f"{rank}\t{_safe_name(row.display_name)}\t{row.user_id}\t"
            f"{row.messages}\t{row.reactions}\t{row.threads}\t{row.total}"
        )

    lines.extend(
        [
            "",
            "NOTES",
            "-" * 72,
            "Readable Discord message and thread history was backfilled before this report.",
            "Historical reaction timestamps are not exposed by Discord; backfilled reactions",
            "are current reactions on messages posted during the selected period.",
            "Bot accounts are excluded. Reaction removals do not erase historical activity.",
            "Custom date-only end dates include that entire local calendar day.",
        ]
    )
    return "\n".join(lines) + "\n"


class EngagementCog(commands.Cog, name="EngagementCog"):
    """Collects member activity and publishes moderator-requested reports."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = EngagementStore(ACTIVITY_DATABASE_PATH)

    async def cog_load(self) -> None:
        await self.store.initialize()
        logger.info("Engagement tracking database ready at %s", ACTIVITY_DATABASE_PATH)

    async def cog_unload(self) -> None:
        await self.store.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        await self.store.record_event(
            guild_id=message.guild.id,
            user_id=message.author.id,
            user_name=message.author.display_name,
            event_type="message",
            occurred_at=message.created_at,
            event_key=f"message:{message.id}",
            channel_id=message.channel.id,
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or (
            self.bot.user is not None and payload.user_id == self.bot.user.id
        ):
            return
        member = payload.member
        if member is None:
            guild = self.bot.get_guild(payload.guild_id)
            member = guild.get_member(payload.user_id) if guild else None
        if member is None or member.bot:
            return
        await self.store.record_event(
            guild_id=payload.guild_id,
            user_id=payload.user_id,
            user_name=member.display_name,
            event_type="reaction",
            occurred_at=datetime.now(timezone.utc),
            event_key=_reaction_event_key(payload.message_id, payload.user_id, payload.emoji),
            channel_id=payload.channel_id,
        )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not thread.guild or thread.owner_id is None:
            return
        member = thread.guild.get_member(thread.owner_id)
        if member is None:
            try:
                member = await thread.guild.fetch_member(thread.owner_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if member.bot:
            return
        await self.store.record_event(
            guild_id=thread.guild.id,
            user_id=member.id,
            user_name=member.display_name,
            event_type="thread",
            occurred_at=thread.created_at,
            event_key=f"thread:{thread.id}",
            channel_id=thread.id,
        )

    async def _scan_message_history(
        self,
        location: discord.TextChannel | discord.Thread,
        *,
        start: datetime,
        end: datetime,
        stats: HistorySyncStats,
    ) -> None:
        """Imports messages and visible reactions from one readable location."""
        pending: list[ActivityEvent] = []
        try:
            async for message in location.history(
                limit=None, after=start, before=end, oldest_first=True
            ):
                stats.messages_scanned += 1
                if not message.author.bot:
                    pending.append(
                        ActivityEvent(
                            guild_id=message.guild.id,
                            user_id=message.author.id,
                            user_name=message.author.display_name,
                            event_type="message",
                            occurred_at=message.created_at,
                            event_key=f"message:{message.id}",
                            channel_id=message.channel.id,
                        )
                    )

                for reaction in message.reactions:
                    try:
                        async for user in reaction.users(limit=None):
                            if user.bot:
                                continue
                            stats.reactions_scanned += 1
                            pending.append(
                                ActivityEvent(
                                    guild_id=message.guild.id,
                                    user_id=user.id,
                                    user_name=getattr(user, "display_name", str(user)),
                                    event_type="reaction",
                                    # Discord does not expose historical reaction timestamps.
                                    # The message timestamp keeps backfilled reactions inside
                                    # the explicitly selected historical period.
                                    occurred_at=message.created_at,
                                    event_key=_reaction_event_key(
                                        message.id, user.id, reaction.emoji
                                    ),
                                    channel_id=message.channel.id,
                                )
                            )
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        logger.warning(
                            "Could not read users for a reaction on message %s",
                            message.id,
                        )

                if len(pending) >= 500:
                    await self.store.record_events(pending)
                    pending.clear()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            stats.locations_skipped += 1
            logger.warning("Could not read message history in channel %s", location.id)
        finally:
            if pending:
                await self.store.record_events(pending)

    async def _discover_threads(
        self,
        guild: discord.Guild,
        *,
        start: datetime,
        end: datetime,
        stats: HistorySyncStats,
    ) -> list[discord.Thread]:
        """Finds active and relevant archived threads visible to the bot."""
        threads: dict[int, discord.Thread] = {}
        try:
            for thread in await guild.active_threads():
                threads[thread.id] = thread
        except (discord.Forbidden, discord.HTTPException):
            stats.locations_skipped += 1

        async def collect(iterator: object) -> None:
            try:
                async for thread in iterator:
                    archive_time = thread.archive_timestamp
                    if archive_time is not None and archive_time < start:
                        break
                    threads[thread.id] = thread
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                stats.locations_skipped += 1

        for channel in guild.text_channels:
            await collect(channel.archived_threads(limit=None, before=end))
            await collect(
                channel.archived_threads(
                    private=True, joined=True, limit=None, before=end
                )
            )
        for channel in guild.forums:
            await collect(channel.archived_threads(limit=None, before=end))

        return list(threads.values())

    async def _record_thread_creation(
        self,
        thread: discord.Thread,
        *,
        start: datetime,
        end: datetime,
    ) -> None:
        if thread.owner_id is None or not (start <= thread.created_at < end):
            return
        owner = thread.owner or thread.guild.get_member(thread.owner_id)
        if owner is None:
            try:
                owner = await thread.guild.fetch_member(thread.owner_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
        if owner.bot:
            return
        await self.store.record_event(
            guild_id=thread.guild.id,
            user_id=owner.id,
            user_name=owner.display_name,
            event_type="thread",
            occurred_at=thread.created_at,
            event_key=f"thread:{thread.id}",
            channel_id=thread.id,
        )

    async def backfill_history(
        self, guild: discord.Guild, start: datetime, end: datetime
    ) -> HistorySyncStats:
        """Backfills every accessible channel and thread for a report window."""
        stats = HistorySyncStats()
        for channel in guild.text_channels:
            await self._scan_message_history(channel, start=start, end=end, stats=stats)
            stats.channels_scanned += 1

        threads = await self._discover_threads(
            guild, start=start, end=end, stats=stats
        )
        for thread in threads:
            await self._record_thread_creation(thread, start=start, end=end)
            await self._scan_message_history(thread, start=start, end=end, stats=stats)
            stats.threads_scanned += 1

        logger.info(
            "History scan for guild %s inspected %s messages across %s channels and %s threads",
            guild.id,
            stats.messages_scanned,
            stats.channels_scanned,
            stats.threads_scanned,
        )
        return stats

    async def publish_report(
        self,
        *,
        guild: discord.Guild,
        start: datetime,
        end: datetime,
        period_label: str,
        requested_by: discord.Member | discord.User,
    ) -> tuple[discord.abc.Messageable, int, int, HistorySyncStats]:
        if REPORT_CHANNEL_ID is None:
            raise RuntimeError("REPORT_CHANNEL_ID is not configured correctly.")

        channel = guild.get_channel(REPORT_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(REPORT_CHANNEL_ID)
            except discord.NotFound as exc:
                raise RuntimeError(
                    f"Report channel {REPORT_CHANNEL_ID} was not found in this server."
                ) from exc
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("The configured report destination is not a text channel.")

        sync_stats = await self.backfill_history(guild, start, end)
        activity = await self.store.aggregate(guild.id, start, end)
        rows = _merge_current_members(guild, activity)
        member_count = guild.member_count or len(guild.members)
        ranking_limit = 300 if member_count <= 300 else 1000
        tracking_started = await self.store.tracking_started_at()
        report = _build_text_report(
            guild=guild,
            rows=rows,
            start=start,
            end=end,
            period_label=period_label,
            requested_by=requested_by,
            tracking_started=tracking_started,
            ranking_limit=ranking_limit,
            sync_stats=sync_stats,
        )

        total_messages = sum(row.messages for row in rows)
        total_reactions = sum(row.reactions for row in rows)
        total_threads = sum(row.threads for row in rows)
        active_rows = [row for row in rows if row.total]
        top_lines = [
            f"**{rank}.** {_safe_name(row.display_name)} — **{row.total:,}** points"
            for rank, row in enumerate(active_rows[:10], start=1)
        ]
        if not top_lines:
            top_lines = ["No qualifying member activity was recorded in this period."]

        embed = discord.Embed(
            title="Member Activity & Engagement Report",
            description=(
                f"Formal server engagement results for **{period_label}**. "
                "The attached text file contains the complete ranked member report."
            ),
            color=BRAND_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Activity Summary",
            value=(
                f"**Messages:** {total_messages:,}\n"
                f"**Reactions:** {total_reactions:,}\n"
                f"**Threads:** {total_threads:,}\n"
                f"**Total points:** {total_messages + total_reactions + total_threads:,}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Report Coverage",
            value=(
                f"**Members evaluated:** {len(rows):,}\n"
                f"**Active members:** {len(active_rows):,}\n"
                f"**Ranking cap:** Top {ranking_limit:,}\n"
                "**Scoring:** 1 point per action"
            ),
            inline=True,
        )
        embed.add_field(
            name="History Scan",
            value=(
                f"**Messages inspected:** {sync_stats.messages_scanned:,}\n"
                f"**Channels:** {sync_stats.channels_scanned:,}\n"
                f"**Threads:** {sync_stats.threads_scanned:,}\n"
                f"**Skipped:** {sync_stats.locations_skipped:,}"
            ),
            inline=False,
        )
        embed.add_field(name="Top Members", value="\n".join(top_lines), inline=False)
        embed.set_footer(text=EMBED_FOOTER_TEXT)

        filename = f"engagement-report-{start:%Y%m%d}-{end:%Y%m%d}.txt"
        attachment = discord.File(
            io.BytesIO(report.encode("utf-8")), filename=filename
        )
        await channel.send(
            content=(
                f"**{BRAND_NAME} formal member engagement report**\n"
                f"Period: **{period_label}** • Requested by {requested_by.mention}"
            ),
            embed=embed,
            file=attachment,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        logger.info(
            "Published engagement report for guild %s covering %s to %s",
            guild.id,
            start,
            end,
        )
        return channel, len(rows), ranking_limit, sync_stats


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EngagementCog(bot))
