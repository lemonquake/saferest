"""
Member onboarding engine: scan, welcome post, warm DM and onboarding log.
Created by Aljay Leodones for SafeRest.

Everything a moderator can change lives in `guild_settings`, so it survives
restarts and never needs a `.env` edit:

▸ the **default welcome channel** - where the public post goes unless a
  different channel is picked for that send,
▸ the **start-here** and **introductions** channels the welcome links to,
▸ the **saved welcome message** used as the starting draft every time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import commands

from config import (
    BRAND_NAME,
    COMMUNITY_DATABASE_PATH,
    INTRODUCTIONS_CHANNEL_ID,
    ONBOARDING_CHANNEL_ID,
    ONBOARDING_LOOKBACK_DAYS,
    WELCOME_CHANNEL_ID,
)
from core.database import get_database
from core.embed_script import clean_text, resolve_text
from utils import embed_builder

logger = logging.getLogger("saferest_bot.onboarding")

# Setting keys stored per guild in `guild_settings`. The channel keys are also
# the placeholder names (`{welcome_channel}` ...), so they must not change.
CHANNEL_SETTINGS = {
    "welcome": "welcome_channel_id",
    "onboarding": "onboarding_channel_id",
    "introductions": "introductions_channel_id",
}
CHANNEL_LABELS = {
    "welcome": "Default welcome channel",
    "onboarding": "Start-here channel",
    "introductions": "Introductions channel",
}
CHANNEL_HINTS = {
    "welcome": "Pre-selected for every welcome post (you can still pick another).",
    "onboarding": "Linked in the welcome post and DM as the place to start.",
    "introductions": "Linked in the welcome post and DM for saying hello.",
}
ENV_CHANNEL_DEFAULTS = {
    "welcome": WELCOME_CHANNEL_ID,
    "onboarding": ONBOARDING_CHANNEL_ID,
    "introductions": INTRODUCTIONS_CHANNEL_ID,
}
MESSAGE_SETTING = "onboarding_welcome_message"

# Built-in welcome post, used until a moderator saves their own.
DEFAULT_MESSAGE = (
    "\U0001F44B **Please give a warm welcome to our newest members!**\n\n"
    "Welcome to the **SafeRest** community — we're thrilled to have you here! \U0001F389\n\n"
    "**◈  Start here →** {onboarding_channel}\n"
    "Everything you need to get comfortable lives there — product guides, care "
    "tips, ordering help and all the essentials.\n\n"
    "**◈  Say hello →** {introductions_channel}\n"
    "Pop in to introduce yourself! Tell us a little about you and what brought "
    "you to SafeRest so the community can connect with you. \U0001F90D"
)

MAX_MESSAGE_LENGTH = 1900
# The editable body, leaving room in the first post for the member tags.
MAX_BODY_LENGTH = 1800

# Delivery modes: (label, emoji, description). Order is the menu order.
DELIVERY_MODES: dict[str, tuple[str, str, str]] = {
    "post_dm": (
        "Post + warm DM",
        "📣",
        "Tag everyone in one post, then DM each member",
    ),
    "post": ("Post only", "📢", "Tag everyone in one post, no DMs"),
    "dm": ("Warm DM only", "✉️", "No public post, just the DM"),
}
DEFAULT_MODE = "post_dm"

DM_PACE_SECONDS = 1.0
PROGRESS_EVERY = 10

ProgressCallback = Callable[[int, int], Awaitable[None]]
PostChannel = discord.TextChannel | discord.Thread


def mode_posts(mode: str) -> bool:
    return mode in ("post_dm", "post")


def mode_dms(mode: str) -> bool:
    return mode in ("post_dm", "dm")


@dataclass(slots=True)
class DayGroup:
    """One selectable join-day bucket shown in the onboarding day picker."""

    key: str
    label: str
    description: str
    members: list[discord.Member] = field(default_factory=list)


@dataclass(slots=True)
class OnboardingSettings:
    """Everything a screen needs, read in a single query."""

    channels: dict[str, int | None]
    # Where each channel came from: "saved" (Mod Panel), "env" or "unset".
    sources: dict[str, str]
    message: str
    custom_message: bool

    @property
    def default_channel_id(self) -> int | None:
        return self.channels.get("welcome")


@dataclass(slots=True)
class DeliveryResult:
    posted_channel: PostChannel | None = None
    jump_url: str | None = None
    dm_success: int = 0
    dm_failed: int = 0
    failed_names: list[str] = field(default_factory=list)
    marked: int = 0


def _bucket_for(days_ago: int) -> tuple[str, str, str]:
    """Maps a join age in days to a (key, label, description) bucket."""
    if days_ago <= 0:
        return "d0", "Joined today", "Members who joined in the last 24 hours"
    if days_ago == 1:
        return "d1", "Joined yesterday", "Members who joined 1 day ago"
    if days_ago <= 6:
        return (
            f"d{days_ago}",
            f"Joined {days_ago} days ago",
            f"Members who joined {days_ago} days ago",
        )
    if days_ago <= 13:
        return "w1", "Joined 1 week ago", "Members who joined 7-13 days ago"
    if days_ago <= 20:
        return "w2", "Joined 2 weeks ago", "Members who joined 14-20 days ago"
    return "w3", "Joined 3+ weeks ago", "Members who joined 21+ days ago"


def _chunk_mentions(
    mentions: list[str], first_budget: int, later_budget: int
) -> list[str]:
    """Groups mentions into space-separated blocks that fit a message."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    budget = first_budget
    for mention in mentions:
        if current and length + len(mention) + 1 > budget:
            chunks.append(" ".join(current))
            current, length = [], 0
            budget = later_budget
        current.append(mention)
        length += len(mention) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks or [""]


def _bucket_order(key: str) -> tuple[int, int]:
    if key.startswith("d"):
        return (0, int(key[1:]))
    return (1, int(key[1:]))


def missing_permissions(channel: PostChannel, member: discord.Member) -> list[str]:
    """Permissions the bot lacks to post the welcome in `channel`."""
    permissions = channel.permissions_for(member)
    needed = [("view_channel", "View Channel")]
    if isinstance(channel, discord.Thread):
        needed.append(("send_messages_in_threads", "Send Messages in Threads"))
    else:
        needed.append(("send_messages", "Send Messages"))
    return [label for attr, label in needed if not getattr(permissions, attr, False)]


async def resolve_post_channel(
    guild: discord.Guild, channel_id: int | None
) -> PostChannel | None:
    """The postable channel behind an id, or `None` if it is gone."""
    if not channel_id:
        return None
    channel = guild.get_channel_or_thread(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None


async def recent_members(
    guild: discord.Guild, exclude_ids: set[int]
) -> list[discord.Member]:
    """Non-bot members who joined in the lookback window, newest first."""
    if not guild.chunked:
        # Without a full member list the scan silently misses people.
        try:
            await guild.chunk(cache=True)
        except (discord.HTTPException, asyncio.TimeoutError):
            logger.warning("Could not load the full member list for %s", guild.id)

    cutoff = datetime.now(timezone.utc) - timedelta(days=ONBOARDING_LOOKBACK_DAYS)
    pending = [
        member
        for member in guild.members
        if not member.bot
        and member.joined_at is not None
        and member.joined_at.astimezone(timezone.utc) >= cutoff
        and member.id not in exclude_ids
    ]
    pending.sort(key=lambda member: member.joined_at, reverse=True)
    return pending


def group_by_day(members: list[discord.Member]) -> list[DayGroup]:
    """Buckets members by how long ago they joined."""
    now = datetime.now(timezone.utc)
    groups: dict[str, DayGroup] = {}
    for member in members:
        days_ago = max((now - member.joined_at.astimezone(timezone.utc)).days, 0)
        key, label, description = _bucket_for(days_ago)
        group = groups.get(key)
        if group is None:
            group = DayGroup(key=key, label=label, description=description)
            groups[key] = group
        group.members.append(member)

    return [groups[key] for key in sorted(groups, key=_bucket_order)]


class OnboardingCog(commands.Cog, name="OnboardingCog"):
    """Scans for un-welcomed members and delivers branded onboarding messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_database(COMMUNITY_DATABASE_PATH)

    async def cog_load(self) -> None:
        await self.db.initialize()
        logger.info("Onboarding ready (lookback: %s days)", ONBOARDING_LOOKBACK_DAYS)

    # -- settings ----------------------------------------------
    async def settings(self, guild_id: int) -> OnboardingSettings:
        """Channels and saved message together, in one round trip."""
        stored = await self.db.get_settings(
            guild_id, [*CHANNEL_SETTINGS.values(), MESSAGE_SETTING]
        )
        channels: dict[str, int | None] = {}
        sources: dict[str, str] = {}
        for key, setting in CHANNEL_SETTINGS.items():
            value = stored.get(setting)
            if value and value.isdigit():
                channels[key], sources[key] = int(value), "saved"
            elif ENV_CHANNEL_DEFAULTS[key]:
                channels[key], sources[key] = ENV_CHANNEL_DEFAULTS[key], "env"
            else:
                channels[key], sources[key] = None, "unset"

        saved = clean_text(stored.get(MESSAGE_SETTING), MAX_BODY_LENGTH)
        return OnboardingSettings(
            channels=channels,
            sources=sources,
            message=saved or DEFAULT_MESSAGE,
            custom_message=bool(saved),
        )

    async def channel_ids(self, guild_id: int) -> dict[str, int | None]:
        """Saved channel choices, falling back to the .env defaults."""
        return (await self.settings(guild_id)).channels

    async def set_channel(self, guild_id: int, key: str, channel_id: int | None) -> None:
        await self.db.set_setting(
            guild_id, CHANNEL_SETTINGS[key], str(channel_id) if channel_id else None
        )

    async def save_message(self, guild_id: int, message: str | None) -> None:
        """Saves the default welcome text; `None` restores the built-in one."""
        text = clean_text(message, MAX_BODY_LENGTH)
        if text == DEFAULT_MESSAGE:
            text = None
        await self.db.set_setting(guild_id, MESSAGE_SETTING, text)

    async def resolve_channel(
        self, guild: discord.Guild, channel_id: int | None
    ) -> PostChannel | None:
        """The postable channel behind an id, or `None` if it is gone."""
        return await resolve_post_channel(guild, channel_id)

    # -- onboarding log ----------------------------------------
    async def onboarded_ids(self, guild_id: int) -> set[int]:
        rows = await self.db.fetchall(
            "SELECT user_id FROM onboarded_members WHERE guild_id = ?", (guild_id,)
        )
        return {row["user_id"] for row in rows}

    async def mark_onboarded(
        self,
        guild_id: int,
        member_ids: list[int],
        *,
        moderator_id: int | None = None,
        method: str = "panel",
    ) -> int:
        if not member_ids:
            return 0
        await self.db.executemany(
            """
            INSERT INTO onboarded_members (user_id, guild_id, onboarded_by, method)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                onboarded_at = datetime('now'),
                onboarded_by = excluded.onboarded_by,
                method = excluded.method
            """,
            [(member_id, guild_id, moderator_id, method) for member_id in member_ids],
        )
        return len(member_ids)

    async def clear_onboarded(self, guild_id: int) -> int:
        """Clears the log so every recent member becomes selectable again."""
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS total FROM onboarded_members WHERE guild_id = ?",
            (guild_id,),
        )
        await self.db.execute(
            "DELETE FROM onboarded_members WHERE guild_id = ?", (guild_id,)
        )
        return row["total"] if row else 0

    async def onboarding_stats(self, guild: discord.Guild) -> dict[str, int]:
        row = await self.db.fetchone(
            """
            SELECT COUNT(*) AS total,
                   SUM(onboarded_at >= datetime('now', '-7 days')) AS recent
            FROM onboarded_members WHERE guild_id = ?
            """,
            (guild.id,),
        )
        pending = await self.pending_members(guild)
        return {
            "total": (row["total"] if row else 0) or 0,
            "last_7_days": (row["recent"] if row else 0) or 0,
            "pending": len(pending),
        }

    # -- scanning ----------------------------------------------
    async def pending_members(self, guild: discord.Guild) -> list[discord.Member]:
        """Non-bot members who joined recently and were never welcomed."""
        return await recent_members(guild, await self.onboarded_ids(guild.id))

    def group_by_day(self, members: list[discord.Member]) -> list[DayGroup]:
        """Buckets pending members by how long ago they joined."""
        return group_by_day(members)

    # -- delivery ----------------------------------------------
    def build_welcome_dm(
        self, member: discord.Member, channels: dict[str, int | None]
    ) -> discord.Embed:
        """Warm DM embed using deep-links so the channels open from a DM."""
        guild = member.guild
        onboarding_link = embed_builder.channel_reference(
            guild.id, channels.get("onboarding"), "start-here"
        )
        intros_link = embed_builder.channel_reference(
            guild.id, channels.get("introductions"), "introductions"
        )

        embed = embed_builder.base_embed(
            title=f"\U0001F44B  Welcome to {BRAND_NAME}!",
            description=(
                f"Hi {member.display_name}, we're so glad you're here! \U0001F389\n\n"
                f"You've just joined the **{guild.name}** community, and we want to "
                "make sure you have the smoothest possible start.\n\n"
                f"{embed_builder.section('Start here')} → {onboarding_link}\n"
                "That's your one-stop hub — product guides, care tips and answers "
                "to the questions we hear most often all live there.\n\n"
                f"{embed_builder.section('Introduce yourself')} → {intros_link}\n"
                "Don't be shy — say hello, tell us a bit about yourself and what "
                "brought you to SafeRest.\n\n"
                f"{embed_builder.SEPARATOR}\n"
                "Our team is always just a message away. Welcome aboard! \U0001F90D"
            ),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    async def dm_welcome(
        self,
        members: list[discord.Member],
        channels: dict[str, int | None],
        progress: Optional[ProgressCallback] = None,
    ) -> tuple[list[discord.Member], list[discord.Member]]:
        """DMs each member, paced for rate limits. Returns (delivered, failed)."""
        delivered: list[discord.Member] = []
        failed: list[discord.Member] = []
        total = len(members)
        for index, member in enumerate(members, start=1):
            try:
                await member.send(embed=self.build_welcome_dm(member, channels))
                delivered.append(member)
            except (discord.Forbidden, discord.HTTPException):
                failed.append(member)
            if progress is not None and index % PROGRESS_EVERY == 0 and index < total:
                try:
                    await progress(index, total)
                except discord.HTTPException:
                    progress = None  # The token expired; keep sending regardless.
            if index < total:
                await asyncio.sleep(DM_PACE_SECONDS)
        return delivered, failed

    async def post_welcome(
        self,
        channel: PostChannel,
        members: list[discord.Member],
        message: str,
        channels: dict[str, int | None],
    ) -> discord.Message:
        """Publishes the welcome post, tagging every selected member."""
        body = resolve_text(message, guild=channel.guild, channels=channels) or ""
        body = body.strip() or (
            resolve_text(DEFAULT_MESSAGE, guild=channel.guild, channels=channels) or ""
        )
        allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
        mentions = [member.mention for member in members]

        # Tag as many members as will fit alongside the message itself, then
        # spill the rest into follow-up messages so nothing exceeds Discord's
        # 2,000 character limit.
        first_budget = MAX_MESSAGE_LENGTH - len(body) - 2
        overflow_budget = MAX_MESSAGE_LENGTH - 40
        if first_budget >= 60:
            chunks = _chunk_mentions(mentions, first_budget, overflow_budget)
            primary = await channel.send(
                content=f"{chunks[0]}\n\n{body}".strip(), allowed_mentions=allowed
            )
            overflow = chunks[1:]
        else:
            primary = await channel.send(
                content=body[:MAX_MESSAGE_LENGTH], allowed_mentions=allowed
            )
            overflow = _chunk_mentions(mentions, overflow_budget, overflow_budget)

        for extra in overflow:
            if extra:
                await channel.send(
                    content=f"…and also welcoming: {extra}", allowed_mentions=allowed
                )
        return primary

    async def deliver(
        self,
        *,
        guild: discord.Guild,
        members: list[discord.Member],
        message: str,
        moderator: discord.Member | discord.User,
        channel: PostChannel | None,
        mode: str,
        channels: dict[str, int | None] | None = None,
        progress: Optional[ProgressCallback] = None,
    ) -> DeliveryResult:
        """Runs the full delivery chain: public post, warm DMs, onboarding log."""
        if mode not in DELIVERY_MODES:
            raise ValueError(f"Unknown delivery mode: {mode}")
        if mode_posts(mode) and channel is None:
            raise ValueError("A channel is required to post the welcome.")
        if channels is None:
            channels = await self.channel_ids(guild.id)
        result = DeliveryResult()

        if mode_posts(mode):
            primary = await self.post_welcome(channel, members, message, channels)
            result.posted_channel = channel
            result.jump_url = primary.jump_url

        welcomed = members
        if mode_dms(mode):
            delivered, failed = await self.dm_welcome(members, channels, progress)
            result.dm_success = len(delivered)
            result.dm_failed = len(failed)
            result.failed_names = [member.display_name for member in failed[:15]]
            if not mode_posts(mode):
                # With no public post, a member whose DMs are closed was never
                # actually welcomed - leave them in the next scan.
                welcomed = delivered

        result.marked = await self.mark_onboarded(
            guild.id,
            [member.id for member in welcomed],
            moderator_id=moderator.id,
            method="dm-only" if mode == "dm" else "panel",
        )
        logger.info(
            "Onboarded %s member(s) in guild %s (mode: %s, post: %s, DMs: %s ok / %s failed)",
            result.marked,
            guild.id,
            mode,
            getattr(channel, "id", None),
            result.dm_success,
            result.dm_failed,
        )
        return result


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnboardingCog(bot))
