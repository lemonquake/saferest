"""
Dynamic text hydration and broadcast message state.
Created by Aljay Leodones for SafeRest.

Any moderator-authored text (onboarding welcome posts, embed broadcasts, plain
messages) passes through `resolve_text` so live placeholders such as
`{server_name}` or `{onboarding_channel}` are substituted before delivery.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import discord

from config import BRAND_NAME, COLOR_BRAND
from core import images
from utils import embed_builder

# Documented placeholders, shown to moderators inside the composer modals.
PLACEHOLDER_HELP = (
    "{user_mention} · {user_name} · {server_name} · {member_count} · "
    "{date_now} · {time_now} · {brand} · {onboarding_channel} · "
    "{introductions_channel} · {welcome_channel}"
)

# Payload schema version. Stored with every saved template and draft so an
# older row can still be recognised (and migrated) by a future build.
# 3 added `image_asset`: a banner uploaded to the bot rather than linked.
PAYLOAD_VERSION = 3

# Field ceilings, kept a little under Discord's own limits.
MAX_CONTENT_LENGTH = 1900
MAX_TITLE_LENGTH = 250
MAX_DESCRIPTION_LENGTH = 3800
MAX_IMAGE_URL_LENGTH = 500


def normalize_color(raw: Any) -> int:
    """Accepts `#1B365D`, `1B365D`, `0x1B365D` or an int; brand navy on failure."""
    if isinstance(raw, bool) or raw is None:
        return COLOR_BRAND
    if isinstance(raw, int):
        value = raw
    else:
        cleaned = str(raw).strip().lstrip("#")
        if cleaned[:2].lower() == "0x":
            cleaned = cleaned[2:]
        try:
            value = int(cleaned, 16)
        except ValueError:
            return COLOR_BRAND
    return value if 0 <= value <= 0xFFFFFF else COLOR_BRAND


def normalize_url(raw: Any) -> Optional[str]:
    """Keeps only `http(s)` links - Discord silently drops anything else."""
    candidate = str(raw or "").strip()
    if not candidate or not candidate.lower().startswith(("http://", "https://")):
        return None
    return candidate[:MAX_IMAGE_URL_LENGTH]


def clean_text(raw: Any, limit: int) -> Optional[str]:
    """Trims a stored or typed field to something Discord will accept."""
    if raw is None:
        return None
    text = str(raw).replace("\r\n", "\n").strip()
    return text[:limit] or None


def _optional_id(raw: Any) -> Optional[int]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_text(
    text: Optional[str],
    *,
    member: Optional[discord.Member] = None,
    guild: Optional[discord.Guild] = None,
    channels: Optional[dict[str, int | None]] = None,
) -> Optional[str]:
    """Replaces every supported placeholder with live server data."""
    if not text or "{" not in text:
        return text

    guild = guild or (member.guild if member else None)
    now = datetime.now(embed_builder.display_timezone())
    channels = channels or {}

    def channel_token(key: str) -> str:
        channel_id = channels.get(key)
        return f"<#{channel_id}>" if channel_id else f"#{key.replace('_', '-')}"

    replacements = {
        "{user_mention}": member.mention if member else "(member mention)",
        "{user_name}": member.display_name if member else "(member name)",
        "{server_name}": guild.name if guild else "(server name)",
        "{member_count}": f"{guild.member_count:,}" if guild and guild.member_count else "0",
        "{date_now}": now.strftime("%d/%m/%Y"),
        "{time_now}": now.strftime("%I:%M %p"),
        "{brand}": BRAND_NAME,
        "{onboarding_channel}": channel_token("onboarding"),
        "{introductions_channel}": channel_token("introductions"),
        "{welcome_channel}": channel_token("welcome"),
    }

    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


class EmbedScript:
    """
    Serializable state for one moderator-composed broadcast.

    Holds the plain message content, a single branded embed and the target
    channels, and can render itself for either a live send or a preview.
    """

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.content: Optional[str] = None
        self.title: Optional[str] = None
        self.description: Optional[str] = None
        # A banner is held one of two ways. `image_asset` is a picture stored
        # in the bot's own asset folder, which posts as an upload the embed
        # points at; `image_url` is a plain link Discord fetches itself. The
        # upload always wins, because a link can expire out from under a
        # saved template.
        self.image_asset: Optional[str] = None
        self.image_url: Optional[str] = None
        self.color: int = COLOR_BRAND
        # The server icon in the embed's top-right corner. On by default so
        # nothing that was already saved changes look, but a message with its
        # own branded banner usually reads better without it.
        self.show_icon: bool = True
        self.channel_ids: list[int] = []
        # Set while the draft is linked to a stored template.
        self.template_id: Optional[int] = None
        self.template_name: Optional[str] = None

    @property
    def is_blank(self) -> bool:
        """True while there is nothing worth previewing, saving or sending."""
        return not (self.content or self.title or self.description or self.has_image)

    @property
    def has_embed(self) -> bool:
        return bool(self.title or self.description or self.has_image)

    @property
    def has_image(self) -> bool:
        return bool(self.image_asset or self.image_url)

    # ── the banner ────────────────────────────────────────────
    def set_image_upload(self, asset_id: str) -> None:
        """Points the banner at a stored upload, dropping any link it had."""
        self.image_asset = asset_id
        self.image_url = None

    def set_image_link(self, url: Optional[str]) -> None:
        """Points the banner at a link, dropping any upload it had."""
        self.image_url = url
        self.image_asset = None

    def clear_image(self) -> None:
        self.image_asset = None
        self.image_url = None

    def resolve_image(self) -> tuple[Optional[str], Optional[Path]]:
        """
        The banner as Discord needs it: `(url for the embed, file to attach)`.

        Both halves are decided in one place so they can never disagree. An
        embed pointing at `attachment://…` with no matching upload is rejected
        outright by Discord, and an upload the embed never names lands under
        the message as a download instead of showing as the picture.
        """
        path = images.path_for(self.image_asset)
        if path is not None:
            return images.reference(path.name), path
        # The stored asset is gone (a wiped data folder, a restore from an
        # older backup): fall back to the link rather than breaking the post.
        if self.image_url:
            return self.image_url, None
        return None, None

    def render_files(self) -> list[discord.File]:
        """Fresh upload handles for the banner - a `File` is spent when sent."""
        _, path = self.resolve_image()
        return [discord.File(path, filename=path.name)] if path is not None else []

    # ── rendering ─────────────────────────────────────────────
    def render_embed(
        self,
        *,
        guild: Optional[discord.Guild] = None,
        channels: Optional[dict[str, int | None]] = None,
        resolve: bool = True,
    ) -> Optional[discord.Embed]:
        """
        Builds the branded embed exactly as Discord will show it.

        `resolve=False` leaves the placeholders as the moderator typed them,
        which is what the Embed Studio wants while a draft is still being
        written — the live substitution belongs to the publish preview.
        """
        if not self.has_embed:
            return None

        def text(value: Optional[str]) -> Optional[str]:
            if not resolve:
                return value
            return resolve_text(value, guild=guild, channels=channels)

        image_url, image_path = self.resolve_image()
        if image_url:
            if image_path is not None and not (self.title or self.description):
                # An uploaded picture on its own posts as the picture itself:
                # full size, with no embed frame around it. The file already
                # travels with the message through `render_files`.
                return None
            # A picture is shown clean - no server icon in the corner and no
            # bot footer or timestamp under it.
            embed = discord.Embed(
                title=text(self.title),
                description=text(self.description),
                color=self.color,
            )
            embed.set_image(url=image_url)
            return embed

        embed = embed_builder.base_embed(
            title=text(self.title),
            description=text(self.description),
            color=self.color,
        )
        if self.show_icon and guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    @property
    def icon_visible(self) -> bool:
        """Whether the server icon actually appears on the posted message."""
        return self.show_icon and not self.has_image

    def render_content(
        self,
        *,
        guild: Optional[discord.Guild] = None,
        channels: Optional[dict[str, int | None]] = None,
    ) -> Optional[str]:
        return resolve_text(self.content, guild=guild, channels=channels)

    # ── serialization ─────────────────────────────────────────
    def to_payload(self) -> dict[str, Any]:
        """
        The message itself, ready to be stored as a template.

        Deliberately excludes the author and the target channels: a template
        is reusable content, not a record of one particular send.
        """
        return {
            "version": PAYLOAD_VERSION,
            "content": self.content,
            "title": self.title,
            "description": self.description,
            "image_asset": self.image_asset,
            "image_url": self.image_url,
            "color": self.color,
            "show_icon": self.show_icon,
        }

    def apply_payload(self, payload: Mapping[str, Any]) -> "EmbedScript":
        """
        Loads a stored payload, sanitising every field on the way in.

        Nothing here trusts the row: an oversized description, a `javascript:`
        image URL or a nonsense colour is corrected rather than breaking the
        composer or being rejected later by Discord.
        """
        self.content = clean_text(payload.get("content"), MAX_CONTENT_LENGTH)
        self.title = clean_text(payload.get("title"), MAX_TITLE_LENGTH)
        self.description = clean_text(
            payload.get("description"), MAX_DESCRIPTION_LENGTH
        )
        # A stored row never gets to name a file on disk: anything that is not
        # a well-formed asset id is dropped, and the link beside it is used.
        stored_asset = payload.get("image_asset")
        self.image_asset = stored_asset if images.is_asset_id(stored_asset) else None
        self.image_url = normalize_url(payload.get("image_url"))
        self.color = normalize_color(payload.get("color"))
        # Templates saved before the icon became a choice have no key at all,
        # and those were all published with the icon showing.
        self.show_icon = bool(payload.get("show_icon", True))
        return self

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, user_id: int
    ) -> "EmbedScript":
        return cls(user_id).apply_payload(payload)

    def to_draft_payload(self) -> dict[str, Any]:
        """Payload plus the template link, for the auto-saved working draft."""
        payload = self.to_payload()
        payload["template_id"] = self.template_id
        payload["template_name"] = self.template_name
        return payload

    def apply_draft_payload(self, payload: Mapping[str, Any]) -> "EmbedScript":
        self.apply_payload(payload)
        self.template_id = _optional_id(payload.get("template_id"))
        self.template_name = clean_text(payload.get("template_name"), 100)
        if self.template_id is None:
            self.template_name = None
        return self

    def clone(self, *, user_id: Optional[int] = None) -> "EmbedScript":
        """Independent copy, so a preview or a send can never mutate the draft."""
        duplicate = EmbedScript(self.user_id if user_id is None else user_id)
        duplicate.apply_draft_payload(self.to_draft_payload())
        duplicate.channel_ids = list(self.channel_ids)
        return duplicate

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_draft_payload()
        payload["user_id"] = self.user_id
        payload["target_channel_ids"] = list(self.channel_ids)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EmbedScript":
        script = cls(_optional_id(payload.get("user_id")) or 0)
        script.apply_draft_payload(payload)
        script.channel_ids = [
            channel_id
            for channel_id in (
                _optional_id(value) for value in payload.get("target_channel_ids", [])
            )
            if channel_id is not None
        ]
        return script
