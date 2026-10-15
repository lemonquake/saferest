"""
Mod Panel Cog for the SafeRest Discord Bot.
Created by Aljay Leodones for SafeRest.
"""

import discord
from discord import app_commands
from discord.ext import commands

from views.saferest_onboarding_view import open_self_onboarding_poster, open_test_channel
from views.common import require_manage

from views.engagement_contest_view import (
    EngagementContestHubView,
    build_contest_hub_embed,
)
from views.member_report_view import MemberReportHubView, build_report_hub_embed
from views.mod_panel_view import ModPanelView, build_mod_panel_embed
from views.onboarding_view import OnboardingHubView, build_hub_embed
from views.role_giver_view import open_role_giver


class ModPanelCog(commands.Cog):
    """Cog handling the management panel and administration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="summon",
        description="Display the persistent SafeRest management dashboard.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def summon_command(self, interaction: discord.Interaction):
        """Summons the persistent Mod Panel."""
        await interaction.response.defer()
        embed = await build_mod_panel_embed(bot=self.bot, guild=interaction.guild)
        await interaction.followup.send(embed=embed, view=ModPanelView())

    @app_commands.command(
        name="onboarding",
        description="Open the onboarding tools.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def onboarding_command(self, interaction: discord.Interaction):
        """Privately opens the moderator onboarding tools."""
        if not await require_manage(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await build_hub_embed(interaction)
        await interaction.followup.send(embed=embed, view=OnboardingHubView(), ephemeral=True)

    @app_commands.command(
        name="test-onboarding",
        description="Open a private onboarding channel to test the flow (yourself or a member).",
    )
    @app_commands.describe(member="Who to onboard. Leave empty to test it on yourself.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def test_onboarding_command(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
        """Opens a SafeRest onboarding channel without adding anyone to a tagged group."""
        if not await require_manage(interaction):
            return
        target = member or interaction.user
        if target.bot:
            await interaction.response.send_message("Bots can't be onboarded.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_test_channel(interaction, target)

    @app_commands.command(
        name="self-onboarding",
        description="Post a public 'Start my onboarding' button so members can onboard themselves.",
    )
    @app_commands.describe(channel="Where to post it. Leave empty to pick from a menu.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def self_onboarding_command(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ):
        """Opens the self-onboarding poster (also in the SafeRest Onboarding hub)."""
        if not await require_manage(interaction):
            return
        await open_self_onboarding_poster(interaction, channel.id if channel else None)

    @app_commands.command(
        name="community-report",
        description="Post the persistent community growth report panel here.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def community_report_command(self, interaction: discord.Interaction):
        """Posts the standalone reporting hub (also reachable from /summon)."""
        await interaction.response.defer()
        embed = await build_report_hub_embed(interaction)
        await interaction.followup.send(embed=embed, view=MemberReportHubView())

    @app_commands.command(
        name="engagement-contest",
        description="Start, publish, end or review the Engagement Contest.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def engagement_contest_command(self, interaction: discord.Interaction):
        """Opens the Engagement Contest hub (also reachable from /summon)."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            embed=await build_contest_hub_embed(interaction),
            view=EngagementContestHubView(),
            ephemeral=True,
        )

    @app_commands.command(
        name="role-giver",
        description="Assign a role to all members in bulk with custom exclusion filters.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def role_giver_command(self, interaction: discord.Interaction):
        """Opens the Role Giver hub (also reachable from /summon)."""
        if not await require_manage(interaction):
            return
        await open_role_giver(interaction)


async def setup(bot: commands.Bot):

    """Adds the ModPanelCog to the bot."""
    await bot.add_cog(ModPanelCog(bot))
