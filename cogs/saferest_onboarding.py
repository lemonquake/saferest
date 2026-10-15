"""
SafeRest Onboarding engine: one private onboarding channel per new member.
Created by Aljay Leodones for SafeRest.

A moderator scans for recent members, picks the join days, and the bot opens
a ticket-style ``#<handle>-onboarding`` channel for each of them in the
onboarding category. Only that member and the server's moderators can see
it. The member is tagged there with the creator questions (✅ YES / ❌ NO,
then 🌱 / 🔥), the community rules and the Slack-connected questionnaire.
Submitting the questionnaire grants the access role that unlocks the rest
of the server. The member or any moderator can close the channel with its
🔒 Close onboarding button, which deletes it.

Every send is remembered as one "tagged group", so the most recent group can
be cleared and tagged again from the scan screen.

Members can also start onboarding themselves: a moderator posts the public
🎟️ Start my onboarding button in any channel, and whoever presses it gets
their own private channel. They are tagged under `SELF_SERVE_BATCH`, so
scans skip them but "clear last tagged group" never picks them up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import re
import time
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import commands

from cogs.creator_onboarding import WELCOME_MESSAGE
from cogs.onboarding import DayGroup, group_by_day, recent_members
from config import (
    COMMUNITY_DATABASE_PATH,
    SAFEREST_ONBOARDING_CATEGORY_ID,
    SAFEREST_ONBOARDING_ROLE_ID,
    STAFF_ROLE_IDS,
    VERIFIED_ROLE_ID,
)
from core.database import get_database

logger = logging.getLogger("saferest_bot.saferest_onboarding")

# Posted above the creator question in every onboarding channel.
TICKET_INTRO = (
    "{mention} 🔒 This is your private onboarding channel — only you and the "
    "SafeRest team can see it. **Finish the onboarding below to unlock all of the "
    "server's channels.** Got a question along the way? Just type it here and "
    "the team will reply."
)
TOPIC_TEMPLATE = "Private SafeRest onboarding for {member} · member ID {member_id}"
_TOPIC_OWNER = re.compile(r"member ID (\d+)")

SETTING_KEYS = {
    "category": "saferest_onboarding_category_id",
    "role": "saferest_onboarding_role_id",
    "verified": "saferest_verified_role_id",
}
ENV_DEFAULTS = {
    "category": SAFEREST_ONBOARDING_CATEGORY_ID,
    "role": SAFEREST_ONBOARDING_ROLE_ID,
    "verified": VERIFIED_ROLE_ID,
}

# Discord allows at most 50 channels in one category.
CATEGORY_LIMIT = 50
CREATE_PACE_SECONDS = 1.0
PROGRESS_EVERY = 5

# Roles with any of these can moderate, so they can see every onboarding channel.
STAFF_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_channels",
    "manage_messages",
    "moderate_members",
)

# The tagged group for members who pressed 🎟️ Start my onboarding themselves.
SELF_SERVE_BATCH = 0

ProgressCallback = Callable[[int, int], Awaitable[None]]


class CategoryFull(Exception):
    """The onboarding category already holds Discord's maximum of 50 channels."""


@dataclass(slots=True)
class SafeRestSettings:
    category_id: int | None
    role_id: int | None
    # Where each value came from: "saved" (Mod Panel), "env" or "unset".
    sources: dict[str, str]
    verified_role_id: int | None = None


@dataclass(slots=True)
class TaggedGroup:
    batch_id: int
    count: int
    tagged_at: float


@dataclass(slots=True)
class TicketResult:
    created: list[discord.TextChannel] = field(default_factory=list)
    reused: list[discord.TextChannel] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (member, reason)
    member_ids: list[int] = field(default_factory=list)  # everyone tagged in a channel

    @property
    def opened(self) -> int:
        return len(self.created) + len(self.reused)


@dataclass(slots=True)
class SelfStartResult:
    # "opened", "existing", "has_access", "busy", "full" or "unavailable".
    status: str
    channel: discord.TextChannel | None = None


def channel_name(member: discord.abc.User) -> str:
    """``lemon`` -> ``lemon-onboarding`` (Discord's channel-name rules)."""
    base = re.sub(r"[^a-z0-9_-]+", "-", (member.name or "").lower()).strip("-_")
    return f"{base[:80] or f'member-{member.id}'}-onboarding"


def owner_from_topic(topic: str | None) -> int | None:
    match = _TOPIC_OWNER.search(topic or "")
    return int(match.group(1)) if match else None


def is_staff_role(role: discord.Role) -> bool:
    if role.is_default() or role.managed:
        return False
    if role.id in STAFF_ROLE_IDS:
        return True
    permissions = role.permissions
    return any(getattr(permissions, name, False) for name in STAFF_PERMISSIONS)


def can_close(
    member: discord.Member, channel: discord.abc.GuildChannel, owner_id: int | None
) -> bool:
    """The channel's own member, or anyone who moderates, may close it."""
    if owner_id is None:
        return False
    if member.id == owner_id:
        return True
    if any(getattr(role, "id", None) in STAFF_ROLE_IDS for role in getattr(member, "roles", ())):
        return True
    permissions = channel.permissions_for(member)
    return any(getattr(permissions, name, False) for name in STAFF_PERMISSIONS)


def ticket_overwrites(
    guild: discord.Guild, member: discord.Member
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Hidden from everyone except the member, the moderators and the bot."""
    staff = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True
    )
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        role: staff for role in guild.roles if is_staff_role(role)
    }
    overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
    overwrites[member] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        embed_links=True,
    )
    overwrites[guild.me] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        embed_links=True,
        manage_channels=True,
    )
    return overwrites


class SafeRestOnboardingCog(commands.Cog, name="SafeRestOnboardingCog"):
    """Opens private onboarding channels and grants access on completion."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_database(COMMUNITY_DATABASE_PATH)
        # (guild, member) pairs whose self-started channel is being opened.
        self._starting: set[tuple[int, int]] = set()

    async def cog_load(self) -> None:
        await self.db.initialize()
        logger.info("SafeRest onboarding ready")

    # -- settings ----------------------------------------------
    async def settings(self, guild_id: int) -> SafeRestSettings:
        stored = await self.db.get_settings(guild_id, list(SETTING_KEYS.values()))
        values: dict[str, int | None] = {}
        sources: dict[str, str] = {}
        for key, setting in SETTING_KEYS.items():
            raw = stored.get(setting)
            if raw and raw.isdigit():
                values[key], sources[key] = int(raw), "saved"
            elif ENV_DEFAULTS[key]:
                values[key], sources[key] = ENV_DEFAULTS[key], "env"
            else:
                values[key], sources[key] = None, "unset"
        return SafeRestSettings(
            values["category"], values["role"], sources, values["verified"]
        )

    async def set_value(self, guild_id: int, key: str, value: int | None) -> None:
        await self.db.set_setting(guild_id, SETTING_KEYS[key], str(value) if value else None)

    @staticmethod
    def category(guild: discord.Guild, settings: SafeRestSettings) -> discord.CategoryChannel | None:
        channel = guild.get_channel(settings.category_id) if settings.category_id else None
        return channel if isinstance(channel, discord.CategoryChannel) else None

    @staticmethod
    def role(guild: discord.Guild, settings: SafeRestSettings) -> discord.Role | None:
        return guild.get_role(settings.role_id) if settings.role_id else None

    def setup_problems(self, guild: discord.Guild, settings: SafeRestSettings) -> list[str]:
        """Everything that would stop channels being opened or access granted."""
        problems: list[str] = []
        me = guild.me
        category = self.category(guild, settings)
        if category is None:
            problems.append("The onboarding **category** was not found — pick one in ⚙️ Settings.")
        elif not category.permissions_for(me).manage_channels:
            problems.append(f"The bot needs **Manage Channels** in the **{category.name}** category.")
        if not me.guild_permissions.manage_roles:
            problems.append("The bot needs the **Manage Roles** permission.")
        role = self.role(guild, settings)
        if role is None:
            problems.append("The **access role** was not found — pick one in ⚙️ Settings.")
        elif role >= me.top_role:
            problems.append(
                f"Move the bot's role **above** {role.mention} in Server Settings → Roles, "
                "so it can hand that role out."
            )
        return problems

    # -- records -----------------------------------------------
    async def tagged_ids(self, guild_id: int) -> set[int]:
        rows = await self.db.fetchall(
            "SELECT user_id FROM saferest_onboarding_members "
            "WHERE guild_id = ? AND batch_id IS NOT NULL",
            (guild_id,),
        )
        return {row["user_id"] for row in rows}

    async def pending_members(
        self, guild: discord.Guild, settings: SafeRestSettings | None = None
    ) -> list[discord.Member]:
        """Recent members never tagged, skipping anyone who already has access."""
        settings = settings or await self.settings(guild.id)
        members = await recent_members(guild, await self.tagged_ids(guild.id))
        role_id = settings.role_id
        return [m for m in members if not any(r.id == role_id for r in m.roles)]

    def group_by_day(self, members: list[discord.Member]) -> list[DayGroup]:
        return group_by_day(members)

    async def last_group(self, guild_id: int) -> TaggedGroup | None:
        row = await self.db.fetchone(
            """
            SELECT batch_id, COUNT(*) AS total, MAX(tagged_at) AS tagged_at
            FROM saferest_onboarding_members
            WHERE guild_id = ? AND batch_id IS NOT NULL AND batch_id != ?
            GROUP BY batch_id ORDER BY MAX(tagged_at) DESC LIMIT 1
            """,
            (guild_id, SELF_SERVE_BATCH),
        )
        if row is None:
            return None
        return TaggedGroup(row["batch_id"], row["total"], row["tagged_at"] or 0.0)

    async def clear_last_group(self, guild_id: int) -> int:
        """Untags the most recent group so those members can be tagged again."""
        group = await self.last_group(guild_id)
        if group is None:
            return 0
        await self.db.execute(
            "UPDATE saferest_onboarding_members SET batch_id = NULL, tagged_at = NULL "
            "WHERE guild_id = ? AND batch_id = ?",
            (guild_id, group.batch_id),
        )
        return group.count

    async def owner_of(self, channel: discord.abc.GuildChannel | discord.Thread | None) -> int | None:
        """The member a SafeRest onboarding channel belongs to, or `None`."""
        if channel is None:
            return None
        owner = owner_from_topic(getattr(channel, "topic", None))
        if owner is not None:
            return owner
        row = await self.db.fetchone(
            "SELECT user_id FROM saferest_onboarding_members WHERE guild_id = ? AND channel_id = ?",
            (channel.guild.id, channel.id),
        )
        return row["user_id"] if row else None

    async def _ticket_channel(self, guild: discord.Guild, user_id: int) -> discord.TextChannel | None:
        row = await self.db.fetchone(
            "SELECT channel_id FROM saferest_onboarding_members WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        channel = guild.get_channel(row["channel_id"]) if row and row["channel_id"] else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _save_ticket(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        *,
        batch_id: int | None,
        tagged_by: int | None,
    ) -> None:
        """Remembers the channel; a test run (no batch) leaves the tag alone."""
        tagged_at = time.time() if batch_id is not None else None
        await self.db.execute(
            """
            INSERT INTO saferest_onboarding_members
                (user_id, guild_id, channel_id, batch_id, tagged_at, tagged_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                batch_id = COALESCE(excluded.batch_id, batch_id),
                tagged_at = COALESCE(excluded.tagged_at, tagged_at),
                tagged_by = COALESCE(excluded.tagged_by, tagged_by)
            """,
            (user_id, guild_id, channel_id, batch_id, tagged_at, tagged_by),
        )

    # -- opening channels --------------------------------------
    async def open_ticket(
        self,
        member: discord.Member,
        category: discord.CategoryChannel,
        view: discord.ui.View,
        *,
        batch_id: int | None,
        tagged_by: int | None,
    ) -> tuple[discord.TextChannel, bool]:
        """
        Opens (or reuses) the member's onboarding channel and tags them in it.

        Returns the channel and whether it was newly created. A member tagged
        again gets the welcome re-posted in the channel they already have.
        """
        guild = member.guild
        channel = await self._ticket_channel(guild, member.id)
        created = channel is None
        if channel is None:
            if len(category.channels) >= CATEGORY_LIMIT:
                raise CategoryFull()
            channel = await guild.create_text_channel(
                channel_name(member),
                category=category,
                overwrites=ticket_overwrites(guild, member),
                topic=TOPIC_TEMPLATE.format(member=member, member_id=member.id),
                reason=f"SafeRest onboarding for {member} ({member.id})",
            )
        await self._save_ticket(
            guild.id, member.id, channel.id, batch_id=batch_id, tagged_by=tagged_by
        )
        await channel.send(
            content=f"{TICKET_INTRO.format(mention=member.mention)}\n\n{WELCOME_MESSAGE}",
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
        )
        return channel, created

    async def deliver(
        self,
        *,
        guild: discord.Guild,
        members: list[discord.Member],
        moderator: discord.abc.User,
        batch_id: int,
        view_factory: Callable[[], discord.ui.View],
        progress: Optional[ProgressCallback] = None,
    ) -> TicketResult:
        """Opens an onboarding channel for every member, paced for rate limits."""
        settings = await self.settings(guild.id)
        category = self.category(guild, settings)
        if category is None:
            raise ValueError("The onboarding category was not found.")
        result = TicketResult()
        total = len(members)
        for index, member in enumerate(members, start=1):
            try:
                channel, created = await self.open_ticket(
                    member, category, view_factory(), batch_id=batch_id, tagged_by=moderator.id
                )
            except CategoryFull:
                result.failed.append((member.display_name, "category is full (50 channels)"))
            except discord.Forbidden:
                result.failed.append((member.display_name, "missing permissions"))
            except discord.HTTPException as exc:
                result.failed.append((member.display_name, str(exc.text or exc)[:80]))
            else:
                (result.created if created else result.reused).append(channel)
                result.member_ids.append(member.id)
            if progress is not None and index % PROGRESS_EVERY == 0 and index < total:
                try:
                    await progress(index, total)
                except discord.HTTPException:
                    progress = None  # The token expired; keep going regardless.
            if index < total:
                await asyncio.sleep(CREATE_PACE_SECONDS)
        logger.info(
            "SafeRest onboarding in guild %s: %s created, %s reused, %s failed",
            guild.id, len(result.created), len(result.reused), len(result.failed),
        )
        return result

    async def self_start(
        self, member: discord.Member, view: discord.ui.View
    ) -> SelfStartResult:
        """
        Opens an onboarding channel for a member who pressed 🎟️ Start my onboarding.

        A member who already has a channel is pointed back to it instead of
        being tagged again, and a double click never opens two channels.
        """
        guild = member.guild
        settings = await self.settings(guild.id)
        role = self.role(guild, settings)
        if role is not None and role in member.roles:
            return SelfStartResult("has_access")
        key = (guild.id, member.id)
        if key in self._starting:
            return SelfStartResult("busy")
        self._starting.add(key)
        try:
            existing = await self._ticket_channel(guild, member.id)
            if existing is not None:
                return SelfStartResult("existing", existing)
            category = self.category(guild, settings)
            if category is None or not category.permissions_for(guild.me).manage_channels:
                logger.warning(
                    "Self-onboarding in guild %s is blocked: category missing or no Manage Channels",
                    guild.id,
                )
                return SelfStartResult("unavailable")
            try:
                channel, _ = await self.open_ticket(
                    member, category, view, batch_id=SELF_SERVE_BATCH, tagged_by=member.id
                )
            except CategoryFull:
                logger.warning("Self-onboarding in guild %s: the category is full", guild.id)
                return SelfStartResult("full")
        finally:
            self._starting.discard(key)
        logger.info("Member %s started onboarding themselves in guild %s", member.id, guild.id)
        return SelfStartResult("opened", channel)

    # -- closing -----------------------------------------------
    async def forget_channel(self, guild_id: int, channel_id: int) -> None:
        """Drops a deleted channel, so tagging that member again opens a new one."""
        await self.db.execute(
            "UPDATE saferest_onboarding_members SET channel_id = NULL "
            "WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )

    async def close_ticket(
        self, channel: discord.TextChannel, *, closed_by: discord.abc.User
    ) -> None:
        """Deletes a member's onboarding channel."""
        await channel.delete(reason=f"Onboarding channel closed by {closed_by} ({closed_by.id})")
        await self.forget_channel(channel.guild.id, channel.id)
        logger.info("Onboarding channel %s closed by %s", channel.id, closed_by.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """A channel deleted by hand in Discord is forgotten too."""
        try:
            await self.forget_channel(channel.guild.id, channel.id)
        except Exception:  # noqa: BLE001 - bookkeeping only
            logger.warning("Could not forget deleted channel %s", channel.id, exc_info=True)

    # -- completion --------------------------------------------
    async def complete(self, member: discord.Member) -> bool:
        """Grants the access role and logs completion. False if it could not."""
        guild = member.guild
        settings = await self.settings(guild.id)
        await self.db.execute(
            """
            INSERT INTO saferest_onboarding_members (user_id, guild_id, completed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET completed_at = excluded.completed_at
            """,
            (member.id, guild.id, time.time()),
        )
        role = self.role(guild, settings)
        if role is None:
            logger.warning("SafeRest onboarding access role %s not found", settings.role_id)
            return False
        if role in member.roles:
            return True
        try:
            await member.add_roles(role, reason="Completed SafeRest onboarding")
        except discord.HTTPException as exc:
            logger.warning("Could not grant %s to %s: %s", role.id, member.id, exc)
            return False
        return True

    async def stats(self, guild: discord.Guild) -> dict[str, int]:
        row = await self.db.fetchone(
            """
            SELECT SUM(channel_id IS NOT NULL) AS channels,
                   SUM(batch_id IS NOT NULL) AS tagged,
                   SUM(completed_at IS NOT NULL) AS completed,
                   SUM(batch_id = ?) AS self_started
            FROM saferest_onboarding_members WHERE guild_id = ?
            """,
            (SELF_SERVE_BATCH, guild.id),
        )
        pending = await self.pending_members(guild)

        def value(key: str) -> int:
            return (row[key] if row else 0) or 0

        return {
            "pending": len(pending),
            "channels": value("channels"),
            "tagged": value("tagged"),
            "completed": value("completed"),
            "self_started": value("self_started"),
        }


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SafeRestOnboardingCog(bot))
