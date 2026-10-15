"""
Shared helpers for the SafeRest moderator interfaces.
Created by Aljay Leodones for SafeRest.
"""

from __future__ import annotations

import discord

from utils import embed_builder

MANAGE_DENIED = (
    "You need the **Manage Server** permission to use this control."
)


def can_manage(interaction: discord.Interaction) -> bool:
    """True when the clicking member may operate the Mod Panel."""
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


async def deny(interaction: discord.Interaction) -> None:
    embed = embed_builder.error_embed("Permission required", MANAGE_DENIED)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def require_manage(interaction: discord.Interaction) -> bool:
    """Guards a control; sends the refusal itself when access is denied."""
    if can_manage(interaction):
        return True
    await deny(interaction)
    return False


def get_cog(interaction: discord.Interaction, name: str):
    return interaction.client.get_cog(name)


async def missing_cog(interaction: discord.Interaction, feature: str) -> None:
    embed = embed_builder.error_embed(
        f"{feature} is unavailable",
        "That module did not load. Check the bot console for the startup error.",
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def ack(interaction: discord.Interaction) -> None:
    """
    Acknowledges a control inside Discord's three-second window.

    Anything that touches the database or the Discord API before replying can
    outrun that window, which makes Discord show "the bot didn't respond in
    time" and throws the real reply away. Deferring first keeps the message the
    control lives on untouched; the handler finishes with `replace_message`.
    """
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer()
    except discord.HTTPException:
        # Already acknowledged elsewhere — nothing left to do.
        pass


async def replace_message(
    interaction: discord.Interaction,
    embed: discord.Embed | list[discord.Embed],
    view: discord.ui.View | None = None,
    attachments: list[discord.File] | None = None,
) -> None:
    """
    Swaps the message behind a control, deferred or not.

    Takes one embed or a list of them, so a screen can pair its own card with
    a live render of whatever the moderator is composing.

    `attachments` is the full set of files the message should carry once the
    edit lands - an uploaded banner has to be re-sent on every redraw, because
    an embed pointing at `attachment://…` is only valid while the file it
    names is actually on the message. `None` leaves whatever is there alone.
    """
    embeds = list(embed) if isinstance(embed, list) else [embed]
    files = discord.utils.MISSING if attachments is None else attachments
    if interaction.response.is_done():
        await interaction.edit_original_response(
            embeds=embeds, view=view, attachments=files
        )
    else:
        await interaction.response.edit_message(
            embeds=embeds, view=view, attachments=files
        )
