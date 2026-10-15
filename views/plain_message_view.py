"""
Plain message composer for the SafeRest Mod Panel.
Created by Aljay Leodones for SafeRest.

**💬 Normal Message** posts as the bot with no embed wrapped around it, so it
reads like any other message in the channel. The composer is one ephemeral hub
that shows the draft exactly as Discord will render it and keeps both the text
and the pictures editable right up until a channel is picked.

Pictures are **uploaded from your computer** - pasting a link still works for
anyone who prefers it. Either way the file is fetched, checked and stored the
moment it is added, so a broken link is caught in the composer instead of in
front of the whole server.

The filename a picture is posted under is built from its actual bytes, never
from the link it came from. That is the difference between a picture and a
grey download card: Discord picks how to show an upload from its extension, so
a WebP called `banner.png` - exactly what a `media.discordapp.net` link hands
you - is offered as a file to download rather than shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Optional, Sequence

import aiohttp
import discord

from config import COLOR_BRAND
from core import images
from core.embed_script import (
    MAX_CONTENT_LENGTH,
    MAX_IMAGE_URL_LENGTH,
    clean_text,
    normalize_url,
    resolve_text,
)
from utils import embed_builder
from views.broadcast_view import TEXT_CHANNEL_TYPES, channel_context
from views.common import ack, require_manage

logger = logging.getLogger("saferest_bot.plain_message")

# Discord lays four attachments out in a tidy grid; past that it stacks them
# and the post stops looking like a normal message.
MAX_IMAGES = 4
COMPOSER_TIMEOUT = 900  # 15 minutes of inactivity

PLACEHOLDER_HINT = "Placeholders: {server_name} {member_count} {onboarding_channel}"
UPLOAD_HINT = (
    f"{images.SUPPORTED_LIST}, up to {images.MAX_IMAGE_MB} MB each. "
    "Pick several at once."
)
LINK_HINT = "One per line. Only if you would rather not upload."


# ── staged pictures ────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class StagedImage:
    """One picture already fetched, checked and stored with the bot."""

    asset_id: str
    label: str
    format: str
    description: str

    def filename(self, index: int) -> str:
        """The name it is posted under - extension from the bytes, always."""
        return images.safe_filename(self.label, self.format, f"image-{index}")


def _unique_names(staged: Sequence[StagedImage]) -> list[str]:
    """
    Posting names, with collisions broken by position.

    Two pictures called `banner.png` would otherwise be indistinguishable in
    the numbered list the composer shows and in the post itself.
    """
    names: list[str] = []
    taken: set[str] = set()
    for index, image in enumerate(staged, start=1):
        name = image.filename(index)
        if name.lower() in taken:
            stem, _, extension = name.rpartition(".")
            name = f"{stem}-{index}.{extension}"
        taken.add(name.lower())
        names.append(name)
    return names


def composer_files(staged: Sequence[StagedImage]) -> list[discord.File]:
    """
    Fresh upload handles for every staged picture.

    A `discord.File` is spent once it is sent, so these are rebuilt for each
    redraw and again for the publish.
    """
    files: list[discord.File] = []
    for name, image in zip(_unique_names(staged), staged):
        path = images.path_for(image.asset_id)
        if path is not None:
            files.append(discord.File(path, filename=name))
    return files


def missing_assets(staged: Sequence[StagedImage]) -> list[str]:
    """Names any staged picture whose stored file has gone missing."""
    return [
        f"**{image.label}**"
        for image in staged
        if images.path_for(image.asset_id) is None
    ]


# ── composer state ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class PlainMessageState:
    """One open plain-message draft."""

    user_id: int
    content: Optional[str] = None
    images: list[StagedImage] = field(default_factory=list)
    channels: dict[str, int | None] = field(default_factory=dict)
    notice: Optional[str] = None
    notice_level: str = "info"

    @property
    def is_blank(self) -> bool:
        """True while there is nothing worth previewing or publishing."""
        return not (self.content or self.images)

    @property
    def free_slots(self) -> int:
        return MAX_IMAGES - len(self.images)

    @property
    def asset_ids(self) -> set[str]:
        return {image.asset_id for image in self.images}

    def render_content(self, guild: discord.Guild | None) -> Optional[str]:
        return resolve_text(self.content, guild=guild, channels=self.channels)

    def inform(self, message: str, level: str = "info") -> None:
        self.notice = message
        self.notice_level = level

    def take_notice(self) -> tuple[Optional[str], str]:
        message, level = self.notice, self.notice_level
        self.notice, self.notice_level = None, "info"
        return message, level


# ── staging ────────────────────────────────────────────────────────────────
def _add(state: PlainMessageState, picture: images.LoadedImage) -> str:
    """Stores one fetched picture on the draft; returns what to report."""
    asset_id = images.store(picture)
    if asset_id in state.asset_ids:
        return "duplicate"
    state.images.append(
        StagedImage(
            asset_id=asset_id,
            label=picture.source_name,
            format=picture.format,
            description=picture.describe(),
        )
    )
    return "added"


async def stage_images(
    state: PlainMessageState,
    *,
    raw: Optional[str] = None,
    uploads: Sequence[discord.Attachment] = (),
) -> tuple[str, str]:
    """
    Fetches, checks and stores everything the moderator just handed over.

    Returns the notice to show and its level. Every picture is downloaded here
    rather than at publish time, so a link that does not work is caught while
    the draft is still open and the reason names the picture it belongs to.
    """
    added = 0
    duplicates = 0
    overflow = 0
    problems: list[str] = []

    for attachment in uploads:
        if state.free_slots <= 0:
            overflow += 1
            continue
        try:
            picture = await images.load_from_attachment(attachment)
        except images.ImageRejected as exc:
            problems.append(f"**{attachment.filename}**: {exc.reason}")
            continue
        outcome = _add(state, picture)
        added += outcome == "added"
        duplicates += outcome == "duplicate"

    links = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    if links:
        timeout = aiohttp.ClientTimeout(total=images.DOWNLOAD_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for line in links:
                if state.free_slots <= 0:
                    overflow += 1
                    continue
                url = normalize_url(line)
                if url is None:
                    problems.append(
                        f"`{line[:40]}`: a link has to start with `http://` "
                        "or `https://`."
                    )
                    continue
                try:
                    picture = await images.load_from_url(url, session=session)
                except images.ImageRejected as exc:
                    problems.append(f"**{images.name_from_url(url)}**: {exc.reason}")
                    continue
                outcome = _add(state, picture)
                added += outcome == "added"
                duplicates += outcome == "duplicate"

    notes: list[str] = []
    if added:
        notes.append(f"Added **{added}** picture(s).")
    if duplicates:
        notes.append(f"**{duplicates}** were already on the draft.")
    if overflow:
        notes.append(
            f"**{overflow}** did not fit — a message carries at most "
            f"{MAX_IMAGES} pictures."
        )
    notes.extend(problems)

    if not notes:
        return "Nothing was added — upload a file or paste a link.", "warning"
    level = "success" if added and not (problems or overflow) else "warning"
    return " ".join(notes), level


def _describe_text(text: Optional[str]) -> str:
    """Summarises the body rather than squashing it onto one line."""
    if not text:
        return "*nothing written yet - press* **Edit message**"
    lines = text.count("\n") + 1
    measure = f"**{len(text):,}** characters"
    if lines > 1:
        measure += f" across **{lines}** lines"
    return f"{measure} - shown above exactly as it will post"


def build_composer_embeds(
    state: PlainMessageState, guild: discord.Guild | None
) -> list[discord.Embed]:
    """The control card, any notice, and a live render of every staged image."""
    card = embed_builder.base_embed(
        title="💬  Normal Message",
        description=(
            f"{embed_builder.SEPARATOR}\n"
            "This posts as plain text — no embed around it. Everything you see "
            "on this screen is what the channel gets. Edit it as often as you "
            "like, then pick a channel below to publish."
        ),
    )
    card.add_field(
        name=embed_builder.section("Message"),
        value=embed_builder.item(_describe_text(state.content)),
        inline=False,
    )

    if state.images:
        names = _unique_names(state.images)
        rows = [
            embed_builder.item(f"**{number}.** `{name}` — {image.description}")
            for number, (name, image) in enumerate(zip(names, state.images), start=1)
        ]
        rows.append(
            embed_builder.item(
                "Saved with the bot and posted as pictures, not downloads "
                f"({state.free_slots} slot(s) left)."
            )
        )
        gone = missing_assets(state.images)
        if gone:
            rows.append(
                embed_builder.item(
                    f"⚠️ {', '.join(gone)} went missing from the bot's files — "
                    "remove and add again."
                )
            )
        picture_list = "\n".join(rows)
    else:
        picture_list = embed_builder.item(
            f"*none yet — add up to {MAX_IMAGES} with* **Add images**"
        )
    card.add_field(
        name=embed_builder.section("Pictures"), value=picture_list, inline=False
    )

    if state.content and ("@everyone" in state.content or "@here" in state.content):
        card.add_field(
            name=embed_builder.section("Heads up"),
            value=embed_builder.item(
                "`@everyone` and `@here` post as plain text here and will "
                "**not** ping the server. Role and member mentions do ping."
            ),
            inline=False,
        )

    embeds = [card]

    notice, level = state.take_notice()
    if notice:
        builder = {
            "success": embed_builder.success_embed,
            "error": embed_builder.error_embed,
            "warning": embed_builder.warning_embed,
        }.get(level, embed_builder.info_embed)
        # Every error this screen can show ends the same way: the draft is
        # still here and nothing reached the channel.
        title = "Nothing was published" if level == "error" else "Draft"
        embeds.append(builder(title, notice))

    # One preview card per picture, each pointing at the copy attached to this
    # very message - so what the moderator sees is the same file the channel
    # will get, not a link that might answer differently later.
    for number, (name, image) in enumerate(
        zip(_unique_names(state.images), state.images), start=1
    ):
        if images.path_for(image.asset_id) is None:
            continue
        preview = discord.Embed(
            description=f"**Picture {number}** — {image.label}",
            color=COLOR_BRAND,
        )
        preview.set_image(url=images.reference(name))
        embeds.append(preview)
    return embeds


async def render_composer(
    interaction: discord.Interaction, state: PlainMessageState
) -> None:
    """Redraws the hub in place: the draft text on top, the controls below."""
    content = state.render_content(interaction.guild)
    embeds = build_composer_embeds(state, interaction.guild)
    view = PlainMessageView(state)
    # Nothing on this screen may ping: it is a preview, and no channel has been
    # chosen yet.
    silent = discord.AllowedMentions.none()
    # The preview cards point at `attachment://…`, which only resolves while
    # the files are on the message, so every redraw carries them again.
    files = composer_files(state.images)
    if interaction.response.is_done():
        await interaction.edit_original_response(
            content=content or None,
            embeds=embeds,
            view=view,
            attachments=files,
            allowed_mentions=silent,
        )
    else:
        await interaction.response.edit_message(
            content=content or None,
            embeds=embeds,
            view=view,
            attachments=files,
            allowed_mentions=silent,
        )


async def _replace_with(interaction: discord.Interaction, embed: discord.Embed) -> None:
    """Retires the composer, leaving one card in its place."""
    if interaction.response.is_done():
        await interaction.edit_original_response(
            content=None, embeds=[embed], view=None, attachments=[]
        )
    else:
        await interaction.response.edit_message(
            content=None, embeds=[embed], view=None, attachments=[]
        )


async def open_composer(
    interaction: discord.Interaction, state: PlainMessageState
) -> None:
    """Sends the composer for the first time, after the opening modal."""
    state.channels = await channel_context(interaction)
    await interaction.followup.send(
        content=state.render_content(interaction.guild) or None,
        embeds=build_composer_embeds(state, interaction.guild),
        view=PlainMessageView(state),
        files=composer_files(state.images),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ── modals ─────────────────────────────────────────────────────────────────
class PlainMessageModal(discord.ui.Modal, title="Normal Message"):
    """
    Writes the message body.

    Opened empty from the Mod Panel, and pre-filled with the current draft
    every time **Edit message** is pressed, so the wording can be reworked as
    many times as it takes.
    """

    def __init__(self, state: Optional[PlainMessageState] = None) -> None:
        super().__init__()
        self.state = state
        self.body = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            placeholder=PLACEHOLDER_HINT,
            default=state.content if state else None,
            max_length=MAX_CONTENT_LENGTH,
            # Optional, so a picture on its own is a valid post.
            required=False,
        )
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        text = clean_text(self.body.value, MAX_CONTENT_LENGTH)

        if self.state is not None:
            await ack(interaction)
            self.state.content = text
            self.state.inform(
                "Message updated." if text else "Message cleared.", "success"
            )
            await render_composer(interaction, self.state)
            return

        # First open: loading the channel settings takes a database round trip,
        # which is more than the three-second reply window allows.
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = PlainMessageState(user_id=interaction.user.id, content=text)
        if state.is_blank:
            state.inform(
                "Nothing written yet. Use **Edit message** for the text, or "
                "**Add images** to post a picture on its own."
            )
        await open_composer(interaction, state)


class AddImagesModal(discord.ui.Modal, title="Add pictures"):
    """Uploads pictures onto the draft, or stages them from links."""

    def __init__(self, state: PlainMessageState) -> None:
        super().__init__()
        self.state = state
        self.uploads = discord.ui.FileUpload(
            required=False, min_values=0, max_values=max(state.free_slots, 1)
        )
        self.urls = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="https://...",
            required=False,
            max_length=MAX_IMAGE_URL_LENGTH * MAX_IMAGES,
        )
        self.add_item(
            discord.ui.Label(
                text=f"Upload pictures ({state.free_slots} slot(s) left)",
                description=UPLOAD_HINT,
                component=self.uploads,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="...or paste image links",
                description=LINK_HINT,
                component=self.urls,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        # Every picture is fetched and stored before the screen comes back,
        # which is well past the three-second reply window.
        await ack(interaction)
        state = self.state
        notice, level = await stage_images(
            state, raw=self.urls.value, uploads=self.uploads.values
        )
        state.inform(notice, level)
        await render_composer(interaction, state)


# ── controls ───────────────────────────────────────────────────────────────
class RemoveImageSelect(discord.ui.Select):
    """Drops one or more staged images off the draft."""

    def __init__(self, state: PlainMessageState) -> None:
        self.state = state
        options = [
            discord.SelectOption(
                label=f"Picture {number}",
                description=image.label[:100],
                value=str(number - 1),
            )
            for number, image in enumerate(state.images, start=1)
        ]
        super().__init__(
            placeholder="Remove a picture...",
            options=options,
            min_values=1,
            max_values=len(options),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        state = self.state
        # Highest index first, so each removal leaves the rest where they are.
        for index in sorted((int(value) for value in self.values), reverse=True):
            if 0 <= index < len(state.images):
                del state.images[index]
        state.inform(f"Removed **{len(self.values)}** picture(s).", "success")
        await render_composer(interaction, state)


class PublishChannelSelect(discord.ui.ChannelSelect):
    """Picks the destination, which is also what publishes the message."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose where to publish this message...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        # Uploading the pictures and posting outruns the three-second reply
        # window, so the click is acknowledged before any of that starts.
        await ack(interaction)
        parent: "PlainMessageView" = self.view  # type: ignore[assignment]
        chosen = self.values[0]

        channel = interaction.guild.get_channel_or_thread(chosen.id)
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(chosen.id)
            except (discord.HTTPException, discord.Forbidden):
                channel = None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            parent.state.inform(
                "That channel could not be opened. Check that the bot can see "
                "it, then try again.",
                "error",
            )
            await render_composer(interaction, parent.state)
            return

        await parent.publish(interaction, channel)


class PlainMessageView(discord.ui.View):
    """The composer hub: edit the text, manage the pictures, then publish."""

    def __init__(self, state: PlainMessageState):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.state = state
        self.add_images.disabled = state.free_slots <= 0
        if state.images:
            self.add_item(RemoveImageSelect(state))
        self.add_item(PublishChannelSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "This draft belongs to someone else",
                    "Open **💬 Normal Message** on the Mod Panel to write your "
                    "own.",
                ),
                ephemeral=True,
            )
            return False
        return await require_manage(interaction)

    # ── row 0: composing ──────────────────────────────────────
    @discord.ui.button(
        label="Edit message", style=discord.ButtonStyle.primary, emoji="✏️", row=0
    )
    async def edit_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(PlainMessageModal(self.state))

    @discord.ui.button(
        label="Add images", style=discord.ButtonStyle.secondary, emoji="🖼️", row=0
    )
    async def add_images(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.state.free_slots <= 0:
            await interaction.response.send_message(
                embed=embed_builder.warning_embed(
                    "No room left",
                    f"A message carries at most {MAX_IMAGES} pictures. Remove "
                    "one below to make space.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AddImagesModal(self.state))

    @discord.ui.button(
        label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️", row=0
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _replace_with(
            interaction,
            embed_builder.info_embed("Cancelled", "Nothing was published."),
        )
        self.stop()

    # ── publishing ────────────────────────────────────────────
    async def publish(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.Thread,
    ) -> None:
        state = self.state
        content = state.render_content(interaction.guild)

        if not content and not state.images:
            state.inform(
                "This draft is empty. Write something with **Edit message**, or "
                "add a picture.",
                "error",
            )
            await render_composer(interaction, state)
            return

        gone = missing_assets(state.images)
        if gone:
            # Nothing goes out with a picture missing - a half-sent message is
            # worse than one the moderator can still fix.
            state.inform(
                f"{', '.join(gone)} went missing from the bot's files. Remove "
                "and add again, then publish.",
                "error",
            )
            await render_composer(interaction, state)
            return

        # Uploading can take a few seconds. Clearing the controls first means a
        # second click cannot post the message twice.
        await interaction.edit_original_response(
            content=None,
            embeds=[
                embed_builder.info_embed(
                    "Publishing",
                    f"Sending to {channel.mention}"
                    + (
                        f" and uploading **{len(state.images)}** picture(s)..."
                        if state.images
                        else "..."
                    ),
                )
            ],
            view=None,
            attachments=[],
        )

        try:
            message = await channel.send(
                content=content,
                files=composer_files(state.images),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True, everyone=False
                ),
            )
        except discord.Forbidden:
            state.inform(
                "The bot needs **View Channel**, **Send Messages** and **Attach "
                f"Files** in {channel.mention}.",
                "error",
            )
            await render_composer(interaction, state)
            return
        except discord.HTTPException as exc:
            logger.exception("Plain message publish failed")
            state.inform(f"Discord rejected the message: {exc}", "error")
            await render_composer(interaction, state)
            return

        logger.info(
            "%s published a plain message to #%s",
            interaction.user,
            getattr(channel, "name", channel.id),
        )
        await _replace_with(
            interaction,
            embed_builder.success_embed(
                "Message published",
                "\n".join(
                    [
                        embed_builder.item(f"Channel: {channel.mention}"),
                        embed_builder.item(f"Pictures: **{len(state.images)}**"),
                        embed_builder.item(
                            f"[Jump to the message]({message.jump_url})"
                        ),
                    ]
                ),
            ),
        )
        self.stop()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        # Without this an unexpected failure leaves the click unanswered, and
        # Discord only reports that the bot did not respond in time.
        logger.exception("Plain message control failed", exc_info=error)
        self.state.inform(
            "That did not go through, and nothing was published. The bot "
            "console has the details.",
            "error",
        )
        try:
            await render_composer(interaction, self.state)
        except discord.HTTPException:
            logger.warning("Could not report a composer failure", exc_info=True)


__all__ = [
    "AddImagesModal",
    "MAX_IMAGES",
    "PlainMessageModal",
    "PlainMessageState",
    "PlainMessageView",
    "StagedImage",
    "build_composer_embeds",
    "composer_files",
    "missing_assets",
    "open_composer",
    "stage_images",
]
