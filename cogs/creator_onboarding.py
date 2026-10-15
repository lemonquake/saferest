"""
Creator onboarding engine: the "Are you a TikTok Shop creator?" welcome.
Created by Aljay Leodones for SafeRest.

Scan for recent members, group them by join day, then open a private
``#<handle>-onboarding`` channel for each (through SafeRest Onboarding) and tag
them there with a ✅ YES / ❌ NO question. Inside a member's own channel the
replies are normal messages, so the conversation - and any question they
type for the team - stays there. A 🧪 test post in a shared channel answers
privately (ephemeral) instead.

It keeps its own onboarding log, so members already welcomed by
Information Onboarding still show up here.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord
from discord.ext import commands

from config import (
    COMMUNITY_DATABASE_PATH,
    CREATOR_GETTING_STARTED_CHANNEL_ID,
    CREATOR_OPPORTUNITIES_CHANNEL_ID,
    CREATOR_WELCOME_CHANNEL_ID,
)
from cogs.onboarding import (
    MAX_MESSAGE_LENGTH,
    DayGroup,
    PostChannel,
    _chunk_mentions,
    group_by_day,
    recent_members,
    resolve_post_channel,
)
from core.database import get_database

logger = logging.getLogger("saferest_bot.creator_onboarding")

# ── Member-facing copy ─────────────────────────────────────────────────────
WELCOME_MESSAGE = (
    "👋 Welcome to the SafeRest Creator Community!\n\n"
    "We're glad you're here. Before we get you set up, we have one quick question:\n\n"
    "**Are you a TikTok Shop creator?**"
)

NOT_A_CREATOR_MESSAGE = (
    "No worries! TikTok Shop is a great way to earn commission just by posting "
    "videos about products you love.\n\n"
    "**Here's how to get started:**\n\n"
    "1. Download TikTok and create an account if you haven't already\n"
    "2. Grow your account to at least 1,000 followers to qualify for TikTok Shop\n"
    "3. Apply to become a TikTok Shop affiliate in the app or at "
    "[shop.tiktok.com](http://shop.tiktok.com)\n"
    "4. Once you're approved, come back, and we'd love to have you in the community!"
)

JOURNEY_QUESTION = (
    "Welcome! One more quick question so we can point you in the right direction:\n"
    "**Where are you in your TikTok Shop journey?**"
)

# `{channel}` becomes the getting-started channel (#🛍️tiktok-shop-101).
LEARNING_MESSAGE = (
    "We'd love to help you hit the ground running! Head to {channel} to get "
    "everything you need to get started with TikTok Shop and SafeRest. Once you've "
    "gone through it, you'll be guided to the next step!"
)

# Shown after 🔥, or once a 🌱 creator has finished TikTok Shop 101. The
# questionnaire (Slack-connected) is attached right below it.
NEXT_STEP_MESSAGE = (
    "## Ready to start earning with SafeRest? 💰 Here's a quick rundown of how this "
    "community works, what we expect from each other, and all the ways you can "
    "make money with us.\n\n"
    "**Community Rules**\n"
    "1. Respect others. No hate speech, discrimination, or bullying.\n"
    "2. Keep chats organized. Use the right channels and avoid spamming.\n"
    "3. Be an active participant. This community grows when everyone "
    "contributes. Share what's working, engage with other creators, and help "
    "each other level up.\n"
    "4. For support, open a ticket in the support channel. "
    "We do not respond to DMs for support requests.\n\n"
    "By joining, you agree to follow these rules.\n\n"
    "## Complete Your Onboarding 👇"
)

FINISHED_101_LABEL = "I've finished TikTok Shop 101"
SHOP_101_CHECKPOINT_MESSAGE = (
    "**Finished TikTok Shop 101?** 🎓\n"
    "Click the button below for your next step."
)

GETTING_STARTED_NAME = "🛍️tiktok-shop-101"

# Sent once the questionnaire is in and the access role is granted.
# `{channel}` becomes #opportunities-to-earn.
COMPLETION_MESSAGE = (
    "You now have full access to the SafeRest creator community! Head to {channel} "
    "to see all the ways you can earn with us. 🎉"
)
OPPORTUNITIES_NAME = "opportunities-to-earn"

# ── Settings ───────────────────────────────────────────────────────────────
CHANNEL_SETTINGS = {
    "welcome": "creator_welcome_channel_id",
    "getting_started": "creator_getting_started_channel_id",
    "opportunities": "creator_opportunities_channel_id",
}
CHANNEL_LABELS = {
    "welcome": "Default test channel",
    "getting_started": "Getting-started channel",
    "opportunities": "Opportunities channel",
}
CHANNEL_HINTS = {
    "welcome": (
        "Pre-selected for 🧪 Test message. Real welcomes go to each member's "
        "private onboarding channel."
    ),
    "getting_started": (
        f"Where 🌱 learning creators are sent (#{GETTING_STARTED_NAME} unless you pick another)."
    ),
    "opportunities": (
        f"Linked in the message members get once they finish (#{OPPORTUNITIES_NAME})."
    ),
}
ENV_CHANNEL_DEFAULTS = {
    "welcome": CREATOR_WELCOME_CHANNEL_ID,
    "getting_started": CREATOR_GETTING_STARTED_CHANNEL_ID,
    "opportunities": CREATOR_OPPORTUNITIES_CHANNEL_ID,
}

JOURNEYS = ("learning", "experienced")


@dataclass(slots=True)
class CreatorSettings:
    channels: dict[str, int | None]
    # Where each channel came from: "saved" (Mod Panel), "env" or "unset".
    sources: dict[str, str]

    @property
    def default_channel_id(self) -> int | None:
        return self.channels.get("welcome")


def getting_started_mention(guild: discord.Guild | None, channel_id: int | None) -> str:
    """The TikTok Shop 101 link: saved channel, else one by that name, else text."""
    if channel_id:
        if guild is None:
            return f"<#{channel_id}>"
        url = f"https://discord.com/channels/{guild.id}/{channel_id}"
        return f"[#{GETTING_STARTED_NAME}]({url})"
    if guild is not None:
        channel = discord.utils.get(guild.text_channels, name=GETTING_STARTED_NAME)
        if channel is not None:
            return channel.mention
    return f"**#{GETTING_STARTED_NAME}**"


def learning_message(guild: discord.Guild | None, channel_id: int | None) -> str:
    return LEARNING_MESSAGE.format(channel=getting_started_mention(guild, channel_id))


def completion_message(guild: discord.Guild | None, channel_id: int | None) -> str:
    """The "you now have full access" message, linking #opportunities-to-earn."""
    if channel_id and guild is not None:
        url = f"https://discord.com/channels/{guild.id}/{channel_id}"
        channel = f"[#{OPPORTUNITIES_NAME}]({url})"
    elif channel_id:
        channel = f"<#{channel_id}>"
    else:
        channel = f"**#{OPPORTUNITIES_NAME}**"
    return COMPLETION_MESSAGE.format(channel=channel)


class CreatorOnboardingCog(commands.Cog, name="CreatorOnboardingCog"):
    """Scans for un-welcomed members and posts the TikTok Shop question."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_database(COMMUNITY_DATABASE_PATH)

    async def cog_load(self) -> None:
        await self.db.initialize()
        logger.info("Creator onboarding ready")

    # -- settings ----------------------------------------------
    async def settings(self, guild_id: int) -> CreatorSettings:
        stored = await self.db.get_settings(guild_id, list(CHANNEL_SETTINGS.values()))
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
        return CreatorSettings(channels=channels, sources=sources)

    async def set_channel(self, guild_id: int, key: str, channel_id: int | None) -> None:
        await self.db.set_setting(
            guild_id, CHANNEL_SETTINGS[key], str(channel_id) if channel_id else None
        )

    async def resolve_channel(
        self, guild: discord.Guild, channel_id: int | None
    ) -> PostChannel | None:
        return await resolve_post_channel(guild, channel_id)

    # -- onboarding log ----------------------------------------
    async def onboarded_ids(self, guild_id: int) -> set[int]:
        rows = await self.db.fetchall(
            "SELECT user_id FROM creator_onboarded_members WHERE guild_id = ?",
            (guild_id,),
        )
        return {row["user_id"] for row in rows}

    async def mark_onboarded(
        self, guild_id: int, member_ids: list[int], *, moderator_id: int | None = None
    ) -> int:
        if not member_ids:
            return 0
        await self.db.executemany(
            """
            INSERT INTO creator_onboarded_members (user_id, guild_id, onboarded_by)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                onboarded_at = datetime('now'),
                onboarded_by = excluded.onboarded_by
            """,
            [(member_id, guild_id, moderator_id) for member_id in member_ids],
        )
        return len(member_ids)

    async def clear_onboarded(self, guild_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS total FROM creator_onboarded_members WHERE guild_id = ?",
            (guild_id,),
        )
        await self.db.execute(
            "DELETE FROM creator_onboarded_members WHERE guild_id = ?", (guild_id,)
        )
        return row["total"] if row else 0

    async def pending_members(self, guild: discord.Guild) -> list[discord.Member]:
        return await recent_members(guild, await self.onboarded_ids(guild.id))

    def group_by_day(self, members: list[discord.Member]) -> list[DayGroup]:
        return group_by_day(members)

    # -- answers -----------------------------------------------
    async def record_answer(
        self, guild_id: int, user_id: int, *, is_creator: bool, journey: str | None = None
    ) -> None:
        """Keeps each member's latest answers; a new YES/NO clears the journey."""
        await self.db.execute(
            """
            INSERT INTO creator_onboarding_answers (user_id, guild_id, is_creator, journey)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                is_creator = excluded.is_creator,
                journey = excluded.journey,
                updated_at = datetime('now')
            """,
            (user_id, guild_id, int(is_creator), journey),
        )

    async def stats(self, guild: discord.Guild) -> dict[str, int]:
        log = await self.db.fetchone(
            """
            SELECT COUNT(*) AS total,
                   SUM(onboarded_at >= datetime('now', '-7 days')) AS recent
            FROM creator_onboarded_members WHERE guild_id = ?
            """,
            (guild.id,),
        )
        answers = await self.db.fetchone(
            """
            SELECT SUM(is_creator = 1) AS yes,
                   SUM(is_creator = 0) AS no,
                   SUM(journey = 'learning') AS learning,
                   SUM(journey = 'experienced') AS experienced
            FROM creator_onboarding_answers WHERE guild_id = ?
            """,
            (guild.id,),
        )
        pending = await self.pending_members(guild)

        def value(row, key: str) -> int:
            return (row[key] if row else 0) or 0

        return {
            "pending": len(pending),
            "total": value(log, "total"),
            "last_7_days": value(log, "recent"),
            "yes": value(answers, "yes"),
            "no": value(answers, "no"),
            "learning": value(answers, "learning"),
            "experienced": value(answers, "experienced"),
        }

    # -- delivery ----------------------------------------------
    async def post_welcome(
        self,
        channel: PostChannel,
        members: list[discord.Member],
        view: discord.ui.View,
    ) -> discord.Message:
        """
        Tags the members, then posts the question with its buttons.

        The buttons always sit on the last message, right below the tags.
        When the tags fit alongside the question it is a single message.
        """
        allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
        budget = MAX_MESSAGE_LENGTH - 40
        chunks = [chunk for chunk in _chunk_mentions(
            [member.mention for member in members], budget, budget
        ) if chunk]

        room = MAX_MESSAGE_LENGTH - len(WELCOME_MESSAGE) - 2
        if len(chunks) <= 1 and (not chunks or len(chunks[0]) <= room):
            tags = chunks[0] if chunks else ""
            return await channel.send(
                content=f"{tags}\n\n{WELCOME_MESSAGE}".strip(),
                view=view,
                allowed_mentions=allowed,
            )

        for chunk in chunks:
            await channel.send(content=chunk, allowed_mentions=allowed)
        return await channel.send(content=WELCOME_MESSAGE, view=view, allowed_mentions=allowed)

    async def deliver(
        self,
        *,
        guild: discord.Guild,
        members: list[discord.Member],
        moderator: discord.Member | discord.User,
        channel: PostChannel,
        view: discord.ui.View,
    ) -> tuple[discord.Message, int]:
        """Posts the creator welcome and logs everyone tagged."""
        primary = await self.post_welcome(channel, members, view)
        marked = await self.mark_onboarded(
            guild.id, [member.id for member in members], moderator_id=moderator.id
        )
        logger.info(
            "Creator-onboarded %s member(s) in guild %s (channel %s)",
            marked, guild.id, channel.id,
        )
        return primary, marked


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CreatorOnboardingCog(bot))
