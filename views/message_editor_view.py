"""
Live message editor for the SafeRest Discord Bot.
Created by Aljay Leodones for SafeRest.

Right-click any message the bot posted and choose **Apps -> Edit Message** to
reopen it in the same composer the Embed Studio uses. The draft starts as a
faithful read-back of what is already on screen; **Update message** writes it
back to the original message in place, so links, pins and reactions survive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import discord

from core.embed_script import EmbedScript, clean_text, normalize_url
from utils import embed_builder
from views.banner import BannerModal, describe_banner
from views.broadcast_view import EmbedComposerModal, channel_context
from views.common import ack, replace_message, require_manage

logger = logging.getLogger("saferest_bot.message_editor")


def _shorten(text: str | None, limit: int, *, fallback: str = "*not set*") -> str:
    if not text:
        return fallback
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _stamp_of(message: discord.Message) -> tuple[str | None, object] | None:
    """The footer text and timestamp a posted message already carries."""
    if not message.embeds:
        return None
    source = message.embeds[0]
    footer_text = source.footer.text if source.footer else None
    return (footer_text, source.timestamp)


def _banner_attachment(
    message: discord.Message, image_url: str | None
) -> discord.Attachment | None:
    """The message's own upload that the embed is using as its banner."""
    if not image_url:
        return None
    for attachment in message.attachments:
        if image_url in {attachment.url, attachment.proxy_url}:
            return attachment
    return None


def script_from_message(
    message: discord.Message, *, user_id: int
) -> tuple[EmbedScript, list[str]]:
    """
    Reads a posted message back into an editable draft.

    The composer only models one branded embed — a title, a body, an image, a
    colour and the line above it. Anything on the message that cannot be
    expressed that way is reported in the returned notes rather than being
    dropped quietly, because it *will* disappear the moment the moderator
    presses **Update message**.
    """
    script = EmbedScript(user_id)
    script.content = clean_text(message.content, 1900)

    notes: list[str] = []
    if len(message.embeds) > 1:
        notes.append(
            f"This message carries **{len(message.embeds)} embeds**. Only the "
            "first one is editable here, and updating removes the rest."
        )

    if message.embeds:
        source = message.embeds[0]
        script.title = clean_text(source.title, 250)
        script.description = clean_text(source.description, 3800)
        script.image_url = normalize_url(source.image.url if source.image else None)
        if source.color is not None:
            script.color = source.color.value
        # Whatever is posted is the truth: a message published without the
        # server icon must not grow one just because it was reopened.
        script.show_icon = bool(source.thumbnail and source.thumbnail.url)

        if source.fields:
            notes.append(
                f"The embed has **{len(source.fields)} field(s)**. The composer "
                "cannot edit fields, so updating removes them — move that text "
                "into the message body first if you need to keep it."
            )
        if source.author and source.author.name:
            notes.append(
                "The embed's author line is replaced by the standard SafeRest "
                "header when you update."
            )

    banner = _banner_attachment(message, script.image_url)
    other_attachments = [
        attachment for attachment in message.attachments if attachment is not banner
    ]
    if other_attachments:
        notes.append(
            f"**{len(other_attachments)} attachment(s)** stay on the message "
            "— editing does not touch uploaded files."
        )
    if message.components:
        notes.append(
            "This message has buttons on it. They keep working after the "
            "update."
        )
    return script, notes


@dataclass(slots=True)
class EditorState:
    """One open editing session against a live message."""

    message: discord.Message
    user_id: int
    script: EmbedScript
    baseline: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    notice: str | None = None
    notice_level: str = "info"
    updated: bool = False
    channels: dict[str, int | None] = field(default_factory=dict)
    # The footer and timestamp the message was originally published with.
    stamp: tuple[str | None, object] | None = None
    # The upload already on the message that its embed uses as the banner,
    # and the link that points at it. Keeping both is what lets an untouched
    # banner survive an edit: the file has to be listed again or Discord
    # strips it, and the embed would then point at nothing.
    banner_attachment: discord.Attachment | None = None
    banner_url: str | None = None

    @property
    def changed(self) -> bool:
        """True while the draft differs from what is actually posted."""
        return self.script.to_payload() != self.baseline

    def adopt(self, script: EmbedScript, notes: list[str]) -> None:
        """Takes a fresh read-back as the new starting point."""
        self.script = script
        self.baseline = script.to_payload()
        self.notes = notes
        self.banner_attachment = _banner_attachment(self.message, script.image_url)
        self.banner_url = script.image_url if self.banner_attachment else None

    def attachments_for_update(self) -> list:
        """
        The complete file list the edited message should end up with.

        Discord replaces the attachments wholesale, so everything that has to
        stay - other uploads, and an untouched banner - is listed again here
        alongside any new banner being uploaded.
        """
        keep = [
            attachment
            for attachment in self.message.attachments
            if attachment is not self.banner_attachment
        ]
        files = self.script.render_files()
        if files:
            return keep + files
        if self.banner_attachment is not None and self.script.image_url == self.banner_url:
            return keep + [self.banner_attachment]
        return keep

    def render(self, guild: discord.Guild | None) -> discord.Embed | None:
        """
        Renders the draft with the message's original footer and timestamp.

        Re-rendering otherwise stamps the embed with the time of the *edit*,
        so correcting a typo would make a week-old post look like it was just
        published.
        """
        embed = self.script.render_embed(guild=guild, channels=self.channels)
        # A picture posts without any footer, so there is nothing to restore.
        if embed is not None and self.stamp is not None and not self.script.has_image:
            footer_text, timestamp = self.stamp
            if footer_text:
                embed.set_footer(text=footer_text)
            embed.timestamp = timestamp
        return embed

    def inform(self, message: str, level: str = "info") -> None:
        self.notice = message
        self.notice_level = level

    def take_notice(self) -> tuple[str | None, str]:
        message, level = self.notice, self.notice_level
        self.notice, self.notice_level = None, "info"
        return message, level


def build_editor_embeds(
    state: EditorState, guild: discord.Guild | None
) -> list[discord.Embed]:
    """The control card, any warnings, and a live render of the result."""
    message = state.message
    script = state.script

    card = embed_builder.base_embed(
        title="✏️  Editing a bot message",
        description=(
            f"{embed_builder.SEPARATOR}\n"
            "Change the wording or the banner, then press **Update message**. "
            "The preview below is what the message becomes."
        ),
    )
    card.add_field(
        name=embed_builder.section("Message"),
        value="\n".join(
            [
                embed_builder.item(f"Channel: {message.channel.mention}"),
                embed_builder.item(f"[Jump to the message]({message.jump_url})"),
                embed_builder.item(
                    "Status: "
                    + (
                        "**unsaved changes**"
                        if state.changed
                        else "matches what is posted"
                    )
                ),
            ]
        ),
        inline=False,
    )
    card.add_field(
        name=embed_builder.section("Text above the embed"),
        value=embed_builder.item(
            _shorten(script.content, 120, fallback="*none*")
        ),
        inline=False,
    )
    card.add_field(
        name=embed_builder.section("Server icon"),
        value=embed_builder.item(
            "Shown in the corner of the embed"
            if script.icon_visible
            else "Hidden — pictures always post without it"
            if script.has_image
            else "Hidden"
        ),
        inline=False,
    )
    card.add_field(
        name=embed_builder.section("Banner image"),
        value=embed_builder.item(
            describe_banner(script, missing="*none — add one with* **Banner image**")
        ),
        inline=False,
    )

    embeds = [card]
    if state.notes:
        embeds.append(
            embed_builder.warning_embed(
                "Before you update",
                "\n".join(embed_builder.item(note) for note in state.notes),
            )
        )

    notice, level = state.take_notice()
    if notice:
        builder = {
            "success": embed_builder.success_embed,
            "error": embed_builder.error_embed,
            "warning": embed_builder.warning_embed,
        }.get(level, embed_builder.info_embed)
        embeds.append(builder("Update", notice))

    preview = state.render(guild)
    if preview is not None:
        embeds.append(preview)
    return embeds


async def render_editor(interaction: discord.Interaction, state: EditorState) -> None:
    await replace_message(
        interaction,
        build_editor_embeds(state, interaction.guild),
        MessageEditorView(state),
        # A staged upload rides along with every redraw: the preview points at
        # `attachment://…`, which only resolves while the file is attached.
        attachments=state.script.render_files(),
    )


async def open_message_editor(
    interaction: discord.Interaction, message: discord.Message
) -> None:
    """
    Entry point for the **Edit Message** context menu.

    Refuses anything the bot could not actually edit before opening a composer
    the moderator would only be able to abandon.
    """
    if not await require_manage(interaction):
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            embed=embed_builder.error_embed(
                "Not available here",
                "Messages can only be edited inside a server.",
            ),
            ephemeral=True,
        )
        return

    if message.author.id != interaction.client.user.id:
        who = "another bot" if message.author.bot else f"**{message.author.display_name}**"
        await interaction.response.send_message(
            embed=embed_builder.error_embed(
                "That message is not mine",
                f"It was posted by {who}. Discord only lets a bot edit the "
                "messages it sent itself — use **Embed Message** on the Mod "
                "Panel to post a new one.",
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    script, notes = script_from_message(message, user_id=interaction.user.id)
    if script.is_blank:
        await interaction.followup.send(
            embed=embed_builder.error_embed(
                "Nothing to edit",
                "This message has no text, embed or image the composer can "
                "work with.",
            ),
            ephemeral=True,
        )
        return

    state = EditorState(message=message, user_id=interaction.user.id, script=script)
    state.adopt(script, notes)
    state.stamp = _stamp_of(message)
    state.channels = await channel_context(interaction)

    await interaction.followup.send(
        embeds=build_editor_embeds(state, interaction.guild),
        view=MessageEditorView(state),
        files=state.script.render_files(),
        ephemeral=True,
    )


class MessageEditorView(discord.ui.View):
    """Composer controls bound to one already-posted message."""

    def __init__(self, state: EditorState):
        super().__init__(timeout=900)
        self.state = state
        self.icon_toggle.label = (
            "Server icon: ON" if state.script.show_icon else "Server icon: OFF"
        )
        # A picture always posts without the icon, so the toggle has nothing to do.
        self.icon_toggle.disabled = state.script.has_image
        self.update_message.disabled = not state.changed
        self.discard.disabled = not state.changed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "This editor belongs to someone else",
                    "Right-click the message and choose **Apps → Edit "
                    "Message** to open your own.",
                ),
                ephemeral=True,
            )
            return False
        return await require_manage(interaction)

    # ── row 0: composing ──────────────────────────────────────
    @discord.ui.button(label="Edit message", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    async def edit_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state

        async def applied(
            modal_interaction: discord.Interaction, notes: list[str]
        ) -> None:
            await ack(modal_interaction)
            state.inform(
                " ".join(["Draft updated.", *notes]) if notes else "Draft updated.",
                "warning" if notes else "success",
            )
            await render_editor(modal_interaction, state)

        # Edits land in `state.script` directly, so the preview and the update
        # always publish exactly what was typed.
        await interaction.response.send_modal(
            EmbedComposerModal(state.script, on_apply=applied)
        )

    @discord.ui.button(label="Banner image", style=discord.ButtonStyle.secondary, emoji="🖼️", row=0)
    async def banner_image(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state

        async def applied(
            modal_interaction: discord.Interaction, notice: str, level: str
        ) -> None:
            state.inform(notice, level)
            await render_editor(modal_interaction, state)

        await interaction.response.send_modal(
            BannerModal(state.script, on_apply=applied)
        )

    @discord.ui.button(label="Server icon: ON", style=discord.ButtonStyle.secondary, emoji="🏷️", row=0)
    async def icon_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Stages showing or hiding the server icon; **Update** applies it."""
        await ack(interaction)
        state = self.state
        state.script.show_icon = not state.script.show_icon
        state.inform(
            "The server icon is staged to show."
            if state.script.show_icon
            else "The server icon is staged to be removed.",
            "success",
        )
        await render_editor(interaction, state)

    # ── row 1: applying ───────────────────────────────────────
    @discord.ui.button(label="Update message", style=discord.ButtonStyle.success, emoji="💾", row=1)
    async def update_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        state = self.state
        embed = state.render(interaction.guild)
        content = state.script.render_content(
            guild=interaction.guild, channels=state.channels
        )
        if embed is None and not content:
            state.inform(
                "There is nothing left to post. Add a title, a message, an "
                "image or a line above the embed first.",
                "error",
            )
            await render_editor(interaction, state)
            return

        try:
            edited = await state.message.edit(
                content=content,
                embed=embed,
                # Discord replaces the file list wholesale on an edit, so the
                # banner - new or untouched - has to be listed again or the
                # embed ends up pointing at an upload that is no longer there.
                attachments=state.attachments_for_update(),
                # An edit never re-notifies for mentions that were already
                # there, and this stops a newly typed @everyone from pinging
                # the server a second time.
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True, everyone=False
                ),
            )
        except discord.Forbidden:
            state.inform(
                "The bot is no longer allowed to edit that message — check "
                f"its permissions in {state.message.channel.mention}.",
                "error",
            )
        except discord.NotFound:
            state.inform(
                "That message has been deleted, so there was nothing to "
                "update.",
                "error",
            )
        except discord.HTTPException as exc:
            logger.exception("Could not edit message %s", state.message.id)
            state.inform(f"Discord rejected the edit: {exc}", "error")
        else:
            state.message = edited
            state.updated = True
            refreshed, notes = script_from_message(edited, user_id=state.user_id)
            state.adopt(refreshed, notes)
            state.inform(
                f"Message updated. [Jump to it]({edited.jump_url})", "success"
            )
            logger.info(
                "%s edited bot message %s in #%s",
                interaction.user,
                edited.id,
                getattr(edited.channel, "name", edited.channel.id),
            )

        await render_editor(interaction, state)

    @discord.ui.button(label="Discard changes", style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
    async def discard(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Re-reads the live message, throwing away everything unsaved."""
        await ack(interaction)
        state = self.state
        try:
            live = await state.message.fetch()
        except discord.NotFound:
            state.inform("That message has been deleted.", "error")
            await render_editor(interaction, state)
            return
        except discord.HTTPException:
            logger.exception("Could not re-read message %s", state.message.id)
            state.inform(
                "The message could not be re-read. Try again in a moment.",
                "error",
            )
            await render_editor(interaction, state)
            return

        state.message = live
        script, notes = script_from_message(live, user_id=state.user_id)
        state.adopt(script, notes)
        state.inform("Back to what is actually posted.")
        await render_editor(interaction, state)

    # ── row 2: closing ────────────────────────────────────────
    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="✖️", row=2)
    async def close(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state
        summary = (
            "The message was updated."
            if state.updated
            else "The message was left exactly as it was."
        )
        if state.changed and not state.updated:
            summary = (
                "Editor closed with **unsaved changes** — the message was not "
                "touched."
            )
        await interaction.response.edit_message(
            embeds=[embed_builder.info_embed("Editor closed", summary)],
            view=None,
        )
        self.stop()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Message editor control failed", exc_info=error)
        self.state.inform(
            "That did not go through, and the message was not changed. The "
            "bot console has the details.",
            "error",
        )
        try:
            await render_editor(interaction, self.state)
        except discord.HTTPException:
            logger.warning("Could not report an editor failure", exc_info=True)


__all__ = [
    "EditorState",
    "MessageEditorView",
    "open_message_editor",
    "script_from_message",
]
