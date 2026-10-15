"""
Picture handling for every moderator-composed message.
Created by Aljay Leodones for SafeRest.

Two things go wrong with images on Discord, and this module exists to stop
both of them:

* **A file whose name disagrees with its bytes is not rendered.** Discord
  decides how to display an upload from its file extension, so a WebP called
  `banner.png` - which is exactly what a `media.discordapp.net` link hands
  you - arrives as a grey download card instead of a picture. Every filename
  built here comes from the *bytes*, never from the link that was pasted.
* **A file attached but never named by the embed sits underneath it.** An
  embed banner has to point at its own upload with `attachment://<filename>`,
  which is what `reference()` returns. Miss that and the picture becomes a
  loose download exactly the way a wrong extension does.

Anything staged here is copied into the asset folder and addressed by the hash
of its contents, so the same picture is only ever stored once, and a saved
announcement keeps its banner long after the Discord link it came from has
expired.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse
import hashlib
import logging
import os
import re

import aiohttp
import discord

from config import BANNER_ASSET_PATH

logger = logging.getLogger("saferest_bot.images")

# The only formats Discord renders inline. Everything else - SVG, HEIC, AVIF,
# TIFF, BMP - comes out as a download card whatever the file is called, which
# is the whole problem this module answers.
EXTENSIONS = {"png": ".png", "jpg": ".jpg", "gif": ".gif", "webp": ".webp"}
FORMAT_NAMES = {"png": "PNG", "jpg": "JPG", "gif": "GIF", "webp": "WebP"}
SUPPORTED_LIST = "PNG, JPG, GIF or WebP"

# The upload ceiling on a server with no boosts.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_MB = MAX_IMAGE_BYTES // (1024 * 1024)
DOWNLOAD_TIMEOUT = 20

# `<24 hex characters><known extension>` and nothing else - a stored payload
# is never trusted to name a file inside the asset folder.
_ASSET_ID = re.compile(r"^[0-9a-f]{24}\.(?:png|jpg|gif|webp)$")


class ImageRejected(Exception):
    """A picture that cannot be used, carrying the sentence that says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LoadedImage:
    """One picture in hand: its bytes, its real format and where it came from."""

    payload: bytes
    format: str
    source_name: str

    @property
    def extension(self) -> str:
        return EXTENSIONS[self.format]

    @property
    def format_name(self) -> str:
        return FORMAT_NAMES[self.format]

    @property
    def size_mb(self) -> float:
        return len(self.payload) / (1024 * 1024)

    def describe(self) -> str:
        """`logo.png - 0.4 MB PNG`, for the confirmation a moderator reads."""
        return f"{self.source_name} - {self.size_mb:.1f} MB {self.format_name}"


# -- reading the bytes ------------------------------------------------------
def detect_format(payload: bytes) -> Optional[str]:
    """The real format from the file's own header, or `None` if unusable."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    return None


def describe_payload(payload: bytes) -> str:
    """Names what a rejected file actually is, so the message is actionable."""
    head = payload[:32]
    if head[:2] == b"BM":
        return "a BMP"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "a TIFF"
    if head[4:8] == b"ftyp":
        brand = head[8:12].decode("ascii", "ignore").lower()
        if brand.startswith(("avif", "avis")):
            return "an AVIF"
        if brand.startswith(("heic", "heif", "heix", "mif1")):
            return "a HEIC photo"
        return "a video"
    if head[:5] == b"%PDF-":
        return "a PDF"
    if head[:2] == b"PK":
        return "a zip archive"
    lowered = payload[:512].lstrip().lower()
    if lowered.startswith(b"<svg") or (
        lowered.startswith(b"<?xml") and b"<svg" in lowered
    ):
        return "an SVG"
    if lowered.startswith((b"<!doctype", b"<html")):
        return "a web page, not the picture on it"
    return "in a format Discord cannot show"


def _accept(payload: bytes) -> str:
    """Validates one picture in hand, returning the format its bytes declare."""
    if not payload:
        raise ImageRejected("that file is empty.")
    if len(payload) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"that picture is over the {MAX_IMAGE_MB} MB upload limit.")
    image_format = detect_format(payload)
    if image_format is None:
        raise ImageRejected(
            f"that file is {describe_payload(payload)} - Discord only shows "
            f"{SUPPORTED_LIST} inline."
        )
    return image_format


def name_from_url(url: str) -> str:
    """The filename a link suggests - used only as a label, never as a path."""
    parsed = urlparse(url)
    return os.path.basename(unquote(parsed.path)).strip() or parsed.netloc or url


async def load_from_url(
    url: str, *, session: Optional[aiohttp.ClientSession] = None
) -> LoadedImage:
    """Downloads one link, or raises `ImageRejected` saying what went wrong."""
    own_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        )
    try:
        async with session.get(url) as response:
            if response.status != 200:
                raise ImageRejected(
                    f"that link answered **HTTP {response.status}**. Discord's "
                    "own image links expire - right-click the picture and "
                    "**Copy Link** again, or upload the file instead."
                )
            declared = response.content_length
            if declared is not None and declared > MAX_IMAGE_BYTES:
                raise ImageRejected(
                    f"that picture is **{declared / (1024 * 1024):.1f} MB**, "
                    f"over the {MAX_IMAGE_MB} MB upload limit."
                )
            # Read with the same ceiling: a server can understate the length,
            # or leave it out entirely.
            payload = await response.content.read(MAX_IMAGE_BYTES + 1)
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("Could not download %s", url, exc_info=True)
        raise ImageRejected("that link could not be opened.") from exc
    finally:
        if own_session:
            await session.close()

    return LoadedImage(
        payload=payload, format=_accept(payload), source_name=name_from_url(url)
    )


async def load_from_attachment(attachment: discord.Attachment) -> LoadedImage:
    """Reads a file a moderator picked in a modal back out of Discord."""
    if attachment.size > MAX_IMAGE_BYTES:
        raise ImageRejected(
            f"**{attachment.filename}** is "
            f"**{attachment.size / (1024 * 1024):.1f} MB**, over the "
            f"{MAX_IMAGE_MB} MB upload limit."
        )
    try:
        payload = await attachment.read()
    except (discord.HTTPException, discord.NotFound) as exc:
        logger.warning("Could not read upload %s", attachment.filename, exc_info=True)
        raise ImageRejected(
            f"**{attachment.filename}** could not be read back from Discord. "
            "Try uploading it again."
        ) from exc

    return LoadedImage(
        payload=payload, format=_accept(payload), source_name=attachment.filename
    )


# -- the asset folder -------------------------------------------------------
def asset_directory() -> Path:
    BANNER_ASSET_PATH.mkdir(parents=True, exist_ok=True)
    return BANNER_ASSET_PATH


def is_asset_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ASSET_ID.match(value))


def store(image: LoadedImage) -> str:
    """
    Copies a picture into the asset folder and returns the id that names it.

    The id is the hash of the contents plus the extension the *bytes* call
    for, which is both what stops the same upload costing anything twice and
    what guarantees Discord renders it instead of offering it as a download.
    """
    asset_id = hashlib.sha256(image.payload).hexdigest()[:24] + image.extension
    path = asset_directory() / asset_id
    if not path.exists():
        # Write beside the target and move it into place, so a crash halfway
        # through can never leave a half-written banner behind.
        staging = path.with_name(path.name + ".part")
        staging.write_bytes(image.payload)
        staging.replace(path)
        logger.info("Stored banner asset %s (%s)", asset_id, image.describe())
    return asset_id


def path_for(asset_id: object) -> Optional[Path]:
    """The file behind an id, or `None` if the id is unknown or malformed."""
    if not is_asset_id(asset_id):
        return None
    path = BANNER_ASSET_PATH / str(asset_id)
    return path if path.is_file() else None


def file_for(asset_id: object) -> Optional[discord.File]:
    """A fresh upload handle - a `discord.File` is spent once it is sent."""
    path = path_for(asset_id)
    return discord.File(path, filename=path.name) if path is not None else None


def reference(asset_id: str) -> str:
    """The `attachment://` URL that makes an embed show its own upload."""
    return f"attachment://{asset_id}"


def safe_filename(source_name: str, image_format: str, fallback: str) -> str:
    """A filename Discord will render: clean stem, extension from the bytes."""
    stem = os.path.splitext(os.path.basename(unquote(source_name or "")))[0]
    stem = "".join(char for char in stem if char.isalnum() or char in "-_")[:60]
    return (stem or fallback) + EXTENSIONS[image_format]


__all__ = [
    "EXTENSIONS",
    "FORMAT_NAMES",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_MB",
    "SUPPORTED_LIST",
    "ImageRejected",
    "LoadedImage",
    "asset_directory",
    "describe_payload",
    "detect_format",
    "file_for",
    "is_asset_id",
    "load_from_attachment",
    "load_from_url",
    "name_from_url",
    "path_for",
    "reference",
    "safe_filename",
    "store",
]
