"""
Weekly announcement cog for the SafeRest Discord Bot.
Created by Aljay Leodones for SafeRest.

Seeds the four recurring announcement templates on startup and runs the
minute-by-minute scheduler that publishes them.
"""

from __future__ import annotations

from datetime import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import COMMUNITY_DATABASE_PATH, REPORT_TIMEZONE
from core.announcements import (
    AnnouncementSchedule,
    get_announcement,
    get_announcement_store,
)
from core.database import get_database
from utils import embed_builder
from views.announcement_view import (
    AnnouncementHubView,
    build_announcement_hub_embed,
)

logger = logging.getLogger("saferest_bot.announcements")


class AnnouncementCog(commands.Cog, name="AnnouncementCog"):
    """Publishes the Monday/Wednesday/Thursday/Friday posts on schedule."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_database(COMMUNITY_DATABASE_PATH)
        self.store = get_announcement_store()

    async def cog_load(self) -> None:
        await self.db.initialize()
        self.scheduler_job.start()
        logger.info(
            "Weekly announcements ready (schedule timezone: %s)", REPORT_TIMEZONE
        )

    async def cog_unload(self) -> None:
        self.scheduler_job.cancel()

    # -- channel placeholders ----------------------------------
    async def _channel_context(self, guild_id: int) -> dict[str, int | None]:
        cog = self.bot.get_cog("OnboardingCog")
        if cog is None:
            return {}
        return await cog.channel_ids(guild_id)

    # -- publishing --------------------------------------------
    async def publish(self, schedule: AnnouncementSchedule) -> discord.Message | None:
        """
        Sends one scheduled announcement.

        Returns the posted message, or `None` when the server, channel or
        permissions are no longer what the schedule expects — the caller logs
        it and moves on rather than taking the whole loop down.
        """
        announcement = get_announcement(schedule.slug)
        guild = self.bot.get_guild(schedule.guild_id)
        if guild is None:
            logger.warning(
                "Skipping %s: the bot is no longer in guild %s",
                schedule.slug,
                schedule.guild_id,
            )
            return None

        channel = guild.get_channel_or_thread(schedule.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning(
                "Skipping %s in %s: channel %s is missing or not a text channel",
                schedule.slug,
                guild.name,
                schedule.channel_id,
            )
            return None

        script = await self.store.script(guild.id, announcement)
        channels = await self._channel_context(guild.id)
        embed = script.render_embed(guild=guild, channels=channels)
        content = script.render_content(guild=guild, channels=channels)
        if embed is None and not content:
            logger.warning("Skipping %s: the saved copy is empty", schedule.slug)
            return None

        try:
            message = await channel.send(
                content=content,
                embed=embed,
                # An uploaded banner travels with the post; the embed points
                # at it by filename, so the file has to be on the message.
                files=script.render_files(),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True, everyone=schedule.mention_everyone
                ),
            )
        except discord.Forbidden:
            logger.warning(
                "Cannot post %s in #%s — the bot needs View Channel, Send "
                "Messages, Embed Links and Attach Files there",
                schedule.slug,
                channel.name,
            )
            return None
        except discord.HTTPException:
            logger.exception("Discord rejected the scheduled %s post", schedule.slug)
            return None

        logger.info(
            "Published %s to #%s in %s", schedule.slug, channel.name, guild.name
        )
        return message

    # -- scheduled jobs ----------------------------------------
    @tasks.loop(minutes=1)
    async def scheduler_job(self) -> None:
        now = datetime.now(embed_builder.display_timezone())
        try:
            pending = await self.store.due(now)
        except Exception:  # noqa: BLE001 - the loop must survive a bad read
            logger.exception("Could not read the announcement schedule")
            return

        for schedule in pending:
            message = await self.publish(schedule)
            # The run is recorded either way. A channel the bot cannot post in
            # would otherwise be retried every minute for the rest of the day.
            await self.store.mark_run(
                guild_id=schedule.guild_id,
                slug=schedule.slug,
                day=now.date().isoformat(),
            )
            if message is None:
                logger.warning(
                    "Scheduled %s did not go out today; fix the channel or "
                    "permissions and post it manually from the Mod Panel",
                    schedule.slug,
                )

    @scheduler_job.before_loop
    async def before_scheduler(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self.store.seed_all(guild.id)
            except Exception:  # noqa: BLE001 - one bad server must not block the rest
                logger.exception(
                    "Could not seed announcement templates for %s", guild.name
                )

    # -- commands ----------------------------------------------
    @app_commands.command(
        name="announcements",
        description="Open the SafeRest Discord Announcement panel in this channel.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def announcements_command(self, interaction: discord.Interaction) -> None:
        """Posts the standalone announcement hub (also on the Mod Panel)."""
        await interaction.response.defer()
        if interaction.guild is not None:
            await self.store.seed_all(interaction.guild.id, actor_id=interaction.user.id)
        embed = await build_announcement_hub_embed(interaction)
        await interaction.followup.send(embed=embed, view=AnnouncementHubView())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AnnouncementCog(bot))
