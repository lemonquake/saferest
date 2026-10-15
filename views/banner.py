"""
The shared banner-image modal.
Created by Aljay Leodones for SafeRest.

Every screen that carries an embed - the weekly announcements, the Embed
Studio and the live message editor - sets its banner through this one modal,
so the behaviour is identical wherever a moderator opens it.

The picture is **uploaded from your computer**; pasting a link still works and
is kept for anyone who prefers it. Either way the bytes are downloaded once,
checked, and stored with the bot, and the embed is pointed at that upload with
`attachment://`. That last step is the difference between a banner and a grey
download card sitting under the message.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

import discord

from core import images
from core.embed_script import (
    MAX_IMAGE_URL_LENGTH,
    EmbedScript,
    clean_text,
    normalize_url,
)
from views.common import ack, require_manage

# (interaction, notice, level) - the screen decides how to show the outcome.
BannerCallback = Callable[[discord.Interaction, str, str], Awaitable[None]]

UPLOAD_HINT = (
    f"{images.SUPPORTED_LIST}, up to {images.MAX_IMAGE_MB} MB. "
    "This is the reliable way."
)
LINK_HINT = "Only if you would rather not upload. Discord's own links expire."
REMOVE_HINT = "Type yes to post with no banner at all."


def describe_banner(script: EmbedScript, *, missing: str) -> str:
    """One line naming the current banner, for a screen's summary card."""
    if script.image_asset:
        if images.path_for(script.image_asset) is None:
            return (
                "⚠️ the uploaded picture is missing from the bot's files — "
                "set it again with **Banner image**"
            )
        return "Uploaded picture — posts as the embed's image, not as a download"
    if script.image_url:
        return f"[Linked image]({script.image_url})"
    return missing


def _wants_removal(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in {"y", "yes", "remove", "none", "off"}


async def apply_banner(
    script: EmbedScript,
    *,
    uploads: list[discord.Attachment],
    typed: Optional[str],
    remove: Optional[str],
) -> tuple[str, str]:
    """
    Writes the moderator's choice onto the draft.

    Returns the notice to show and its level. The draft is left exactly as it
    was whenever the answer is contradictory or the picture cannot be used -
    a half-applied banner is worse than one the moderator can retry.
    """
    link = clean_text(typed, MAX_IMAGE_URL_LENGTH)
    removing = _wants_removal(remove)

    if removing and (uploads or link):
        return (
            "Pick one: a new banner, or removing the current one. Nothing was "
            "changed.",
            "warning",
        )
    if removing:
        if not script.has_image:
            return "There was no banner to remove.", "info"
        script.clear_image()
        return "Banner removed. The embed posts without a picture.", "success"

    if uploads:
        try:
            picture = await images.load_from_attachment(uploads[0])
        except images.ImageRejected as exc:
            return f"That upload was not used — {exc.reason}", "error"
        script.set_image_upload(images.store(picture))
        return (
            f"Banner set from your upload — **{picture.describe()}**. It posts "
            "as the embed's image, not as a download.",
            "success",
        )

    if link:
        url = normalize_url(link)
        if url is None:
            return (
                "That link was ignored — an image link has to start with "
                "`http://` or `https://`.",
                "error",
            )
        try:
            picture = await images.load_from_url(url)
        except images.ImageRejected as exc:
            return (
                f"That link was not used — {exc.reason} Upload the file "
                "instead and it will always work.",
                "error",
            )
        # The link is copied rather than trusted: the picture then survives
        # the link expiring, and it is checked once here instead of failing
        # silently in front of the whole server.
        script.set_image_upload(images.store(picture))
        return (
            f"Banner set from that link — **{picture.describe()}**. The "
            "picture is now saved with the bot, so the link expiring cannot "
            "break it.",
            "success",
        )

    return "Nothing was filled in, so the banner is unchanged.", "info"


class BannerModal(discord.ui.Modal, title="Banner image"):
    """Sets, replaces or removes the banner on one draft."""

    def __init__(self, script: EmbedScript, *, on_apply: BannerCallback) -> None:
        super().__init__()
        self.script = script
        self._on_apply = on_apply

        self.upload = discord.ui.FileUpload(
            required=False, min_values=0, max_values=1
        )
        self.url = discord.ui.TextInput(
            placeholder="https://...",
            default=script.image_url,
            required=False,
            max_length=MAX_IMAGE_URL_LENGTH,
        )
        self.remove = discord.ui.TextInput(
            placeholder="no",
            required=False,
            max_length=10,
        )
        self.add_item(
            discord.ui.Label(
                text="Upload a picture",
                description=UPLOAD_HINT,
                component=self.upload,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="...or paste an image link",
                description=LINK_HINT,
                component=self.url,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Remove the current banner?",
                description=REMOVE_HINT,
                component=self.remove,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        # Downloading and storing the picture outruns the three-second reply
        # window, so the submit is acknowledged before any of that starts.
        await ack(interaction)
        notice, level = await apply_banner(
            self.script,
            uploads=self.upload.values,
            typed=self.url.value,
            remove=self.remove.value,
        )
        await self._on_apply(interaction, notice, level)


__all__ = ["BannerModal", "apply_banner", "describe_banner"]
