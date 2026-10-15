"""
Embed broadcast composer for the SafeRest Mod Panel.
Created by Aljay Leodones for SafeRest.

The composer modal edits an `EmbedScript` in place, which lets the Embed Studio
(see `views/embed_studio_view.py`) reopen it pre-filled, save it as a template
and publish it, all from the same draft.

Plain, embed-free posts have their own composer in
`views/plain_message_view.py`.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

import discord

from core import images
from core.embed_script import (
    MAX_CONTENT_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_IMAGE_URL_LENGTH,
    MAX_TITLE_LENGTH,
    EmbedScript,
    clean_text,
    normalize_color,
    normalize_url,
)
from utils import embed_builder
from views.common import ack, replace_message, require_manage

logger = logging.getLogger("saferest_bot.broadcast_ui")

TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]

PLACEHOLDER_HINT = "Placeholders: {server_name} {member_count} {onboarding_channel}"

# Called after the composer modal writes the moderator's edits into the draft.
# The list carries anything that had to be corrected on the way in.
ApplyCallback = Callable[[discord.Interaction, list[str]], Awaitable[None]]


async def channel_context(interaction: discord.Interaction) -> dict[str, int | None]:
    cog = interaction.client.get_cog("OnboardingCog")
    if cog is None or interaction.guild is None:
        return {}
    return await cog.channel_ids(interaction.guild.id)


class BroadcastChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose where to publish this message...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        # Publishing reads the server settings and posts to Discord, which is
        # more work than the three-second reply window allows. Acknowledge the
        # click first, then edit the preview with the outcome.
        await ack(interaction)
        parent: "BroadcastPreviewView" = self.view  # type: ignore[assignment]
        chosen = self.values[0]
        channel = interaction.guild.get_channel_or_thread(chosen.id)
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(chosen.id)
            except (discord.HTTPException, discord.Forbidden):
                channel = None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await replace_message(
                interaction,
                embed_builder.error_embed(
                    "Channel unavailable",
                    "That channel could not be opened. Check that the bot can "
                    "see it, then try again.",
                ),
            )
            parent.stop()
            return
        await parent.publish(interaction, channel)


class BroadcastPreviewView(discord.ui.View):
    """Shows the composed message and publishes it to the chosen channel."""

    def __init__(self, script: EmbedScript, *, mention_everyone: bool = False):
        super().__init__(timeout=600)
        # A copy, so editing the draft afterwards can never change what this
        # preview publishes.
        self.script = script.clone()
        # Off unless a caller opts in: an accidental @everyone cannot be taken
        # back, so a draft that merely contains the word never pings on its own.
        self.mention_everyone = mention_everyone
        self.add_item(BroadcastChannelSelect())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=embed_builder.info_embed("Cancelled", "Nothing was published."),
            view=None,
            attachments=[],
        )
        self.stop()

    async def publish(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.Thread,
    ) -> None:
        channels = await channel_context(interaction)
        embed = self.script.render_embed(guild=interaction.guild, channels=channels)
        content = self.script.render_content(guild=interaction.guild, channels=channels)
        if embed is None and not content:
            await self._finish(
                interaction,
                embed_builder.error_embed(
                    "Nothing to send",
                    "This draft is empty. Reopen **Edit embed** and fill in a "
                    "title, a message, an image or a line above the embed.",
                ),
            )
            return

        try:
            message = await channel.send(
                content=content,
                embed=embed,
                # An uploaded banner is sent with the message and named by the
                # embed; without the file it would be a broken reference, and
                # without the reference a download card under the post.
                files=self.script.render_files(),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True, everyone=self.mention_everyone
                ),
            )
        except discord.Forbidden:
            await self._finish(
                interaction,
                embed_builder.error_embed(
                    "Missing permissions",
                    "The bot needs **View Channel**, **Send Messages**, "
                    f"**Embed Links** and **Attach Files** in {channel.mention}.",
                ),
            )
            return
        except discord.HTTPException as exc:
            logger.exception("Broadcast failed")
            await self._finish(
                interaction,
                embed_builder.error_embed(
                    "Could not publish", f"Discord rejected the message: {exc}"
                ),
            )
            return

        await self._finish(
            interaction,
            embed_builder.success_embed(
                "Message published",
                "\n".join(
                    [
                        embed_builder.item(f"Channel: {channel.mention}"),
                        embed_builder.item(f"[Jump to the message]({message.jump_url})"),
                    ]
                ),
            ),
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        # Without this, an unexpected failure leaves the click unanswered and
        # Discord only says the bot did not respond in time.
        logger.exception("Broadcast control failed", exc_info=error)
        await self._finish(
            interaction,
            embed_builder.error_embed(
                "Something went wrong",
                "The message was not published. The bot console has the "
                "details.",
            ),
        )

    async def _finish(
        self, interaction: discord.Interaction, embed: discord.Embed
    ) -> None:
        """Reports the outcome on the preview and retires the controls."""
        try:
            # The preview's banner upload goes with it - the outcome card has
            # no picture, and a leftover file would sit under it.
            await replace_message(interaction, embed, view=None, attachments=[])
        except discord.HTTPException:
            # The preview was dismissed while the message was on its way out;
            # the publish itself already succeeded or failed on its own merits.
            logger.warning("Could not update the broadcast preview", exc_info=True)
        self.stop()


async def show_preview(
    interaction: discord.Interaction,
    script: EmbedScript,
    *,
    notes: Optional[list[str]] = None,
    mention_everyone: bool = False,
    intro_title: str = "Preview",
) -> None:
    """Sends the private preview with the publish controls attached."""
    # Loading the saved channel settings takes a database round trip, so the
    # click is acknowledged before that work starts.
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    channels = await channel_context(interaction)
    embed = script.render_embed(guild=interaction.guild, channels=channels)
    content = script.render_content(guild=interaction.guild, channels=channels)

    lines = [
        "This is exactly how the message will look. Pick a channel below to "
        "publish it."
    ]
    if script.template_name:
        lines.append(embed_builder.item(f"Template: **{script.template_name}**"))
    if mention_everyone:
        lines.append(
            embed_builder.item(
                "This post **will ping @everyone** when it is published."
            )
        )
    if notes:
        lines.extend(embed_builder.item(note) for note in notes)

    intro = embed_builder.info_embed(intro_title, "\n".join(lines))
    embeds = [intro] + ([embed] if embed else [])
    await interaction.followup.send(
        content=content,
        embeds=embeds,
        files=script.render_files(),
        view=BroadcastPreviewView(script, mention_everyone=mention_everyone),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class EmbedComposerModal(discord.ui.Modal, title="Embed Message"):
    """
    Edits one embed draft.

    Every field is optional on its own — a title, a body, an image or just a
    plain line above the embed is enough — but at least one has to be filled.
    """

    def __init__(
        self,
        script: Optional[EmbedScript] = None,
        *,
        on_apply: Optional[ApplyCallback] = None,
    ) -> None:
        super().__init__()
        self.script = script if script is not None else EmbedScript(0)
        self._on_apply = on_apply

        self.heading = discord.ui.TextInput(
            label="Title",
            placeholder="Example: New arrivals this week",
            default=self.script.title,
            max_length=MAX_TITLE_LENGTH,
            required=False,
        )
        self.body = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            placeholder=PLACEHOLDER_HINT,
            default=self.script.description,
            max_length=MAX_DESCRIPTION_LENGTH,
            required=False,
        )
        self.lead = discord.ui.TextInput(
            label="Text above the embed (optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Pings and short intros go here, e.g. @everyone",
            default=self.script.content,
            max_length=MAX_CONTENT_LENGTH,
            required=False,
        )
        # Deliberately not a way to *remove* the banner: an uploaded one
        # cannot be shown in a text box, so a blank field means "leave it
        # alone" rather than silently deleting a picture the moderator cannot
        # see. Removing is what the Banner image screen is for.
        self.image_url = discord.ui.TextInput(
            label="Image link — blank keeps the banner",
            placeholder="Use the 🖼️ Banner image button to upload or remove one",
            default=self.script.image_url,
            required=False,
            max_length=MAX_IMAGE_URL_LENGTH,
        )
        self.color = discord.ui.TextInput(
            label="Colour hex (optional)",
            placeholder="Leave blank for SafeRest navy, or use e.g. C8A96A",
            default=f"#{self.script.color:06X}" if self.script.color else None,
            required=False,
            max_length=9,
        )
        for field in (self.heading, self.body, self.lead, self.image_url, self.color):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return

        title = clean_text(self.heading.value, MAX_TITLE_LENGTH)
        description = clean_text(self.body.value, MAX_DESCRIPTION_LENGTH)
        lead = clean_text(self.lead.value, MAX_CONTENT_LENGTH)
        typed_image = clean_text(self.image_url.value, MAX_IMAGE_URL_LENGTH)

        if not (title or description or lead or typed_image or self.script.has_image):
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "Nothing to send",
                    "Fill in at least a title, a message, a line above the "
                    "embed or a banner image.",
                ),
                ephemeral=True,
            )
            return

        # Storing a newly typed link means downloading it, which outruns the
        # three-second reply window the modal is answered in.
        await interaction.response.defer()
        notes = await self._apply_image(typed_image)

        self.script.user_id = interaction.user.id
        self.script.title = title
        self.script.description = description
        self.script.content = lead
        self.script.color = normalize_color(self.color.value)

        if self._on_apply is not None:
            await self._on_apply(interaction, notes)
            return
        await show_preview(interaction, self.script, notes=notes)

    async def _apply_image(self, typed: Optional[str]) -> list[str]:
        """
        Applies whatever is in the image box, reporting anything it could not.

        A link that is already the draft's banner is left alone, so editing a
        title does not re-download the picture every time.
        """
        if not typed or typed == self.script.image_url:
            return []

        url = normalize_url(typed)
        if url is None:
            return [
                "The image link was ignored — Discord only accepts links "
                "that start with `http://` or `https://`. The banner is "
                "unchanged."
            ]
        try:
            picture = await images.load_from_url(url)
        except images.ImageRejected as exc:
            return [
                f"The image link was not used — {exc.reason} The banner is "
                "unchanged; **Banner image** can upload the file directly."
            ]
        # Copied rather than linked, so the banner survives the link expiring
        # and is checked here instead of failing in front of the server.
        self.script.set_image_upload(images.store(picture))
        return []
