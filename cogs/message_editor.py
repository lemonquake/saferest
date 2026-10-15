"""
Message editing cog for the SafeRest Discord Bot.
Created by Aljay Leodones for SafeRest.

Adds the **Edit Message** right-click action, which reopens any message the
bot posted in the branded composer.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from views.message_editor_view import open_message_editor

logger = logging.getLogger("saferest_bot.message_editor")

CONTEXT_MENU_NAME = "Edit Message"


class MessageEditorCog(commands.Cog, name="MessageEditorCog"):
    """Hosts the message context menu."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Context menus cannot be declared with a decorator inside a cog, so
        # the command is built here and attached to the tree on load.
        self.edit_menu = app_commands.ContextMenu(
            name=CONTEXT_MENU_NAME,
            callback=self.edit_message_menu,
        )
        self.edit_menu.default_permissions = discord.Permissions(manage_guild=True)
        self.edit_menu.allowed_contexts = app_commands.AppCommandContext(
            guild=True, dm_channel=False, private_channel=False
        )

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.edit_menu)
        logger.info("Right-click %r is registered", CONTEXT_MENU_NAME)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self.edit_menu.name, type=self.edit_menu.type
        )

    async def edit_message_menu(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """Opens the editor for the right-clicked message."""
        await open_message_editor(interaction, message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MessageEditorCog(bot))
