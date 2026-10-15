"""
Unified SafeRest embed design system.
Created by Aljay Leodones for SafeRest.

Every embed in the bot is produced here so that colours, section markers,
separators and footers stay identical across onboarding, reporting and
broadcasts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from config import (
    BOT_NAME,
    COLOR_ACCENT,
    COLOR_BRAND,
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    REPORT_TIMEZONE,
)

# ── Visual formatting standards ────────────────────────────────────────────
SECTION_MARK = "◈"
ITEM_MARK = "▸"
SEPARATOR = "━" * 35

BADGE_HEALTHY = "✅"
BADGE_WARNING = "⚠️"
BADGE_ERROR = "❌"

_ANSI_RESET = "\u001b[0m"
_ANSI_YELLOW = "\u001b[0;33m"
_ANSI_GREEN = "\u001b[0;32m"
_ANSI_RED = "\u001b[0;31m"

HEALTHY_STATES = {"online", "ready", "ok", "healthy", "connected", "enabled"}


def display_timezone() -> ZoneInfo:
    """Timezone used for human-facing timestamps (falls back to UTC)."""
    try:
        return ZoneInfo(REPORT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def footer_text() -> str:
    """`SafeRest Bot • DD/MM/YYYY HH:MM AM/PM`"""
    stamp = datetime.now(display_timezone()).strftime("%d/%m/%Y %I:%M %p")
    return f"{BOT_NAME} • {stamp}"


def section(title: str) -> str:
    """Section header, e.g. `◈  Getting Started`."""
    return f"{SECTION_MARK}  {title}"


def item(text: str) -> str:
    """List item, e.g. `▸ Guides and resources`."""
    return f"{ITEM_MARK} {text}"


def base_embed(
    *,
    title: str | None = None,
    description: str | None = None,
    color: int = COLOR_BRAND,
    url: str | None = None,
) -> discord.Embed:
    """Creates a branded embed with the standard footer and UTC timestamp."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        url=url,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=footer_text())
    return embed


def success_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title=f"{BADGE_HEALTHY}  {title}", description=description, color=COLOR_SUCCESS)


def error_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title=f"{BADGE_ERROR}  {title}", description=description, color=COLOR_ERROR)


def warning_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title=f"{BADGE_WARNING}  {title}", description=description, color=COLOR_WARNING)


def info_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title=f"ℹ️  {title}", description=description, color=COLOR_INFO)


def accent_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title=title, description=description, color=COLOR_ACCENT)


def status_block(components: dict[str, str]) -> str:
    """Renders a component/state map inside a Discord ANSI code block."""
    lines = ["```ansi"]
    for label, state in components.items():
        normalized = str(state).strip().lower()
        if normalized in HEALTHY_STATES:
            prefix, colour = "✓", _ANSI_GREEN
        elif normalized in {"off", "disabled", "unavailable", "not configured"}:
            prefix, colour = "✗", _ANSI_RED
        else:
            prefix, colour = "•", _ANSI_YELLOW
        lines.append(f"{prefix} {label:<24} {colour}{state}{_ANSI_RESET}")
    lines.append("```")
    return "\n".join(lines)


def delta_line(label: str, current: int, previous: int | None) -> str:
    """`Members   │  1180 →  1185  📈 +5 (+0.4%)` growth row."""
    if previous is None:
        return f"{label:<10}│ {current:>6}   (no prior data)"

    delta = current - previous
    if delta > 0:
        arrow, sign = "📈", f"+{delta}"
    elif delta < 0:
        arrow, sign = "📉", f"{delta}"
    else:
        arrow, sign = "➖", "±0"

    pct = f" ({delta / previous * 100:+.1f}%)" if previous > 0 else ""
    return f"{label:<10}│ {previous:>6} → {current:<6} {arrow} {sign}{pct}"


def code_block(lines: list[str], language: str = "text") -> str:
    body = "\n".join(lines) if lines else "No data available."
    return f"```{language}\n{body}\n```"


def channel_reference(guild_id: int | None, channel_id: int | None, label: str) -> str:
    """
    Returns a clickable reference for a channel.

    Inside a guild a normal `<#id>` mention is used; for DMs a full
    `discord.com/channels/...` deep-link is produced so the link still works.
    """
    if channel_id is None:
        return f"`#{label}` *(not configured yet)*"
    if guild_id is None:
        return f"<#{channel_id}>"
    return f"[#{label}](https://discord.com/channels/{guild_id}/{channel_id})"
