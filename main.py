"""
Main entry point for the SafeRest Discord Bot.
Created by Aljay Leodones for SafeRest.
"""

import logging
import sys

import discord
from discord.ext import commands

from config import (
    BRAND_NAME,
    DISCORD_TOKEN,
    ENABLE_PRESENCE_INTENT,
    GUILD_ID,
)
from views.announcement_view import AnnouncementHubView
from views.saferest_onboarding_view import SafeRestOnboardingHubView, SelfOnboardingView
from views.creator_onboarding_view import (
    CreatorJourneyView,
    CreatorOnboardingHubView,
    CreatorQuestionnaireView,
    CreatorWelcomeView,
    OnboardingTicketView,
    Shop101DoneView,
)
from views.member_report_view import MemberReportHubView
from views.mod_panel_view import ModPanelView
from views.onboarding_view import OnboardingHubView

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("saferest_bot")

EXTENSIONS = (
    "cogs.engagement",
    "cogs.engagement_contest",
    "cogs.onboarding",
    "cogs.creator_onboarding",
    "cogs.saferest_onboarding",
    "cogs.questionnaire",
    "cogs.memberreport",
    "cogs.announcements",
    "cogs.message_editor",
    "cogs.mod_panel",
)


class SafeRestBot(commands.Bot):
    """Custom Bot class for SafeRest management."""

    def __init__(self, *, use_presences: bool = True):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        intents.presences = use_presences

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        """Initializes persistent views, cogs, and slash command trees."""
        logger.info("Registering persistent views...")
        # Static custom_ids keep every panel button alive across restarts.
        self.add_view(ModPanelView())
        self.add_view(OnboardingHubView())
        self.add_view(CreatorOnboardingHubView())
        self.add_view(SafeRestOnboardingHubView())
        # Member-facing creator buttons, shared by the public Onboarding post
        # and every private SafeRest onboarding channel.
        self.add_view(CreatorWelcomeView())
        # YES / NO / 🔒 Close on the first post in a private onboarding channel.
        self.add_view(OnboardingTicketView())
        # The public 🎟️ Start my onboarding button, wherever it was posted.
        self.add_view(SelfOnboardingView())
        self.add_view(CreatorJourneyView())
        self.add_view(Shop101DoneView())
        self.add_view(CreatorQuestionnaireView())
        self.add_view(MemberReportHubView())
        self.add_view(AnnouncementHubView())

        logger.info("Loading extensions...")
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        # Command synchronization
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            logger.info("Synced %d command(s) to test guild ID: %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global command(s).", len(synced))

    async def on_ready(self):
        """Triggered when the bot has connected to Discord."""
        logger.info("==========================================")
        logger.info("Logged in as: %s (ID: %s)", self.user.name, self.user.id)
        logger.info("Bot is active in %d server(s)", len(self.guilds))
        logger.info(
            "Presence tracking: %s", "on" if self.intents.presences else "off"
        )
        logger.info("==========================================")

        # Set bot presence status
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{BRAND_NAME} Server",
        )
        await self.change_presence(
            status=discord.Status.online,
            activity=activity,
        )


def run():
    """Validates configuration and launches the bot."""
    if not DISCORD_TOKEN:
        logger.error(
            "DISCORD_TOKEN is missing! Please configure it in your .env file."
        )
        sys.exit(1)

    try:
        SafeRestBot(use_presences=ENABLE_PRESENCE_INTENT).run(DISCORD_TOKEN)
    except discord.PrivilegedIntentsRequired:
        # The Presence Intent switch is off in the Discord Developer Portal.
        # Everything except the "Online now" metric works without it, so the
        # bot restarts itself instead of refusing to start.
        logger.warning(
            "Presence Intent is disabled in the Discord Developer Portal. "
            "Starting without it — enable it there to track online members."
        )
        SafeRestBot(use_presences=False).run(DISCORD_TOKEN)


if __name__ == "__main__":
    run()
