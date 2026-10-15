"""
Moderator interface for the SafeRest onboarding system.
Created by Aljay Leodones for SafeRest.

Flow:  Scan  ->  one welcome composer  ->  Send.

The composer is a single ephemeral screen with everything on it: which join
days to include, **which channel to post in** (pre-filled with the server's
default welcome channel), how to deliver it and the message itself. Nothing
goes out until **Send** is pressed, so any of it can be changed freely first.

The default channel and the default message can be saved straight from the
composer, or from **⚙️ Settings** on the hub.

Every control acknowledges Discord before it touches the database, so a slow
query can never turn into "This interaction failed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Optional

import discord

from cogs.onboarding import (
    CHANNEL_HINTS,
    CHANNEL_LABELS,
    DEFAULT_MESSAGE,
    DEFAULT_MODE,
    DELIVERY_MODES,
    DM_PACE_SECONDS,
    MAX_BODY_LENGTH,
    DayGroup,
    OnboardingSettings,
    missing_permissions,
    mode_dms,
    mode_posts,
    resolve_post_channel,
)
from config import ONBOARDING_LOOKBACK_DAYS
from core.embed_script import PLACEHOLDER_HELP, clean_text, resolve_text
from utils import embed_builder
from views.common import ack, missing_cog, replace_message, require_manage

logger = logging.getLogger("saferest_bot.onboarding_ui")

TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
COMPOSER_TIMEOUT = 900  # 15 minutes of inactivity
PLACEHOLDER_HINT = "{onboarding_channel} {introductions_channel} {server_name}"

NOTICE_STYLES = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}
SOURCE_LABELS = {"saved": "", "env": " *(from .env)*", "unset": ""}


def _onboarding_cog(interaction: discord.Interaction):
    return interaction.client.get_cog("OnboardingCog")


def _member_preview(members: list[discord.Member], limit: int = 12) -> str:
    names = [member.display_name for member in members[:limit]]
    extra = len(members) - len(names)
    preview = ", ".join(names) if names else "No members selected yet"
    return f"{preview} … and {extra} more" if extra > 0 else preview


def _channel_line(channel_id: int | None, source: str = "saved") -> str:
    if not channel_id:
        return "*not set*"
    return f"<#{channel_id}>{SOURCE_LABELS.get(source, '')}"


# ── Composer state ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class WelcomeDraft:
    """One moderator's welcome, from scan to send."""

    user_id: int
    groups: list[DayGroup]
    settings: OnboardingSettings
    message: str
    channel_id: int | None
    mode: str = DEFAULT_MODE
    selected_keys: set[str] = field(default_factory=lambda: {"all"})
    notice: tuple[str, str] | None = None

    @classmethod
    def start(
        cls, user_id: int, groups: list[DayGroup], settings: OnboardingSettings
    ) -> "WelcomeDraft":
        return cls(
            user_id=user_id,
            groups=groups,
            settings=settings,
            message=settings.message,
            channel_id=settings.default_channel_id,
        )

    # -- selection ---------------------------------------------
    def apply_selection(self, keys: set[str]) -> None:
        # "Everyone" wins over individual days, so the menu never shows both.
        self.selected_keys = {"all"} if not keys or "all" in keys else keys

    def is_active(self, key: str) -> bool:
        return "all" in self.selected_keys or key in self.selected_keys

    @property
    def selected_members(self) -> list[discord.Member]:
        chosen = [
            member
            for group in self.groups
            if self.is_active(group.key)
            for member in group.members
        ]
        unique: dict[int, discord.Member] = {member.id: member for member in chosen}
        return sorted(unique.values(), key=lambda m: m.joined_at, reverse=True)

    # -- channel & message -------------------------------------
    @property
    def is_default_channel(self) -> bool:
        return bool(self.channel_id) and self.channel_id == self.settings.default_channel_id

    @property
    def message_is_saved(self) -> bool:
        return self.message == self.settings.message

    @property
    def message_status(self) -> str:
        if not self.message_is_saved:
            return "Edited for this send *(not saved as default)*"
        if self.settings.custom_message:
            return "Your saved default message"
        return "Built-in SafeRest message"

    # -- feedback ----------------------------------------------
    def inform(self, message: str, level: str = "info") -> None:
        self.notice = (level, message)

    def take_notice(self) -> tuple[str, str] | None:
        notice, self.notice = self.notice, None
        return notice


def build_composer_embeds(
    draft: WelcomeDraft, guild: discord.Guild | None
) -> list[discord.Embed]:
    """The control card plus a live preview of the public post."""
    members = draft.selected_members
    notice = draft.take_notice()
    lines: list[str] = []
    if notice:
        level, text = notice
        lines += [f"{NOTICE_STYLES.get(level, 'ℹ️')} {text}", embed_builder.SEPARATOR]

    lines += [
        embed_builder.section("Members") + f" **{len(members)}** selected",
        _member_preview(members),
        "",
        "\n".join(
            embed_builder.item(
                f"**{group.label}** — {len(group.members)}"
                + ("  ✅" if draft.is_active(group.key) else "")
            )
            for group in draft.groups
        ),
        "",
    ]

    label, emoji, _ = DELIVERY_MODES[draft.mode]
    lines.append(embed_builder.section("Delivery") + f" {emoji} **{label}**")
    if mode_posts(draft.mode):
        if draft.channel_id:
            tag = "  ⭐ *default*" if draft.is_default_channel else ""
            lines.append(embed_builder.item(f"Posting in <#{draft.channel_id}>{tag}"))
        else:
            lines.append(
                embed_builder.item(
                    "⚠️ **No channel chosen** — pick one in the channel menu."
                )
            )
    if mode_dms(draft.mode):
        seconds = int(len(members) * DM_PACE_SECONDS)
        lines.append(
            embed_builder.item(f"Warm DMs paced at 1/sec (about {seconds}s)")
        )
    lines += [
        embed_builder.item(f"Message: {draft.message_status}"),
        embed_builder.SEPARATOR,
        "Pick the days, the channel and the delivery below, then press **Send**. "
        "Use **⭐ Make default** to remember the channel for next time.",
    ]

    control = embed_builder.base_embed(
        title="👋  Welcome new members", description="\n".join(lines)
    )

    if mode_posts(draft.mode):
        body = resolve_text(draft.message, guild=guild, channels=draft.settings.channels)
        tags = " ".join(f"@{member.display_name}" for member in members[:8])
        if len(members) > 8:
            tags += f" … +{len(members) - 8} more"
        preview = embed_builder.accent_embed(
            "Post preview",
            f"*{tags or 'No members tagged'}*\n\n{(body or '').strip()}"[:4000],
        )
    else:
        preview = embed_builder.accent_embed(
            "Post preview",
            "*No public post — each member only gets the warm DM.*",
        )
    return [control, preview]


async def render_composer(interaction: discord.Interaction, draft: WelcomeDraft) -> None:
    await replace_message(
        interaction, build_composer_embeds(draft, interaction.guild), WelcomeComposerView(draft)
    )


# ── Composer controls ──────────────────────────────────────────────────────
class DaySelect(discord.ui.Select):
    def __init__(self, draft: WelcomeDraft):
        total = sum(len(group.members) for group in draft.groups)
        options = [
            discord.SelectOption(
                label=f"Everyone not yet welcomed ({total})",
                value="all",
                emoji="✅",
                description=f"All members from the last {ONBOARDING_LOOKBACK_DAYS} days",
                default="all" in draft.selected_keys,
            )
        ]
        for group in draft.groups[:24]:
            options.append(
                discord.SelectOption(
                    label=f"{group.label} ({len(group.members)})",
                    value=group.key,
                    emoji="📅",
                    description=group.description[:100],
                    default=group.key in draft.selected_keys,
                )
            )
        super().__init__(
            placeholder="Which join days to welcome (pick several)...",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        draft: WelcomeDraft = self.view.draft  # type: ignore[attr-defined]
        draft.apply_selection(set(self.values))
        await render_composer(interaction, draft)


class DestinationSelect(discord.ui.ChannelSelect):
    """Chooses where this welcome is posted; opens on the default channel."""

    def __init__(self, draft: WelcomeDraft):
        super().__init__(
            placeholder="Choose the channel to post the welcome in...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            default_values=[discord.Object(draft.channel_id)] if draft.channel_id else [],
            disabled=not mode_posts(draft.mode),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        draft: WelcomeDraft = self.view.draft  # type: ignore[attr-defined]
        cog = _onboarding_cog(interaction)
        channel = await cog.resolve_channel(interaction.guild, self.values[0].id) if cog else None
        if channel is None:
            draft.inform("That channel could not be opened. Pick another.", "error")
        else:
            missing = missing_permissions(channel, interaction.guild.me)
            draft.channel_id = channel.id
            if missing:
                draft.inform(
                    f"The bot is missing **{', '.join(missing)}** in {channel.mention}. "
                    "Fix that before sending, or pick another channel.",
                    "warning",
                )
            elif draft.is_default_channel:
                draft.inform(f"Posting in {channel.mention} (your default).", "success")
            else:
                draft.inform(
                    f"Posting in {channel.mention}. Press **⭐ Make default** to "
                    "use it every time.",
                    "success",
                )
        await render_composer(interaction, draft)


class ModeSelect(discord.ui.Select):
    def __init__(self, draft: WelcomeDraft):
        super().__init__(
            placeholder="How should the welcome be delivered?",
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    emoji=emoji,
                    description=description,
                    default=key == draft.mode,
                )
                for key, (label, emoji, description) in DELIVERY_MODES.items()
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        draft: WelcomeDraft = self.view.draft  # type: ignore[attr-defined]
        draft.mode = self.values[0]
        await render_composer(interaction, draft)


class DraftMessageModal(discord.ui.Modal, title="Welcome message"):
    """Edits the message for this send only (it can be saved afterwards)."""

    def __init__(self, draft: WelcomeDraft):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.draft = draft
        self.body = discord.ui.TextInput(
            label="Welcome post (members are tagged above it)",
            style=discord.TextStyle.paragraph,
            default=draft.message[:MAX_BODY_LENGTH],
            max_length=MAX_BODY_LENGTH,
            required=True,
            placeholder=PLACEHOLDER_HINT,
        )
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        await ack(interaction)
        text = clean_text(self.body.value, MAX_BODY_LENGTH)
        if text:
            self.draft.message = text
            self.draft.inform(
                "Message updated for this send. Press **💾 Save message** to "
                "keep it as the default.",
                "success",
            )
        else:
            self.draft.inform("The message cannot be empty — nothing changed.", "warning")
        await render_composer(interaction, self.draft)


class WelcomeComposerView(discord.ui.View):
    """The single-screen welcome composer."""

    def __init__(self, draft: WelcomeDraft):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.draft = draft
        self.add_item(DaySelect(draft))
        self.add_item(DestinationSelect(draft))
        self.add_item(ModeSelect(draft))

        posts = mode_posts(draft.mode)
        self.make_default.disabled = not posts or not draft.channel_id or draft.is_default_channel
        self.save_message.disabled = draft.message_is_saved
        self.revert_message.disabled = draft.message_is_saved
        count = len(draft.selected_members)
        self.send.label = f"Send to {count} member{'s' if count != 1 else ''}"
        self.send.disabled = count == 0 or (posts and not draft.channel_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.draft.user_id:
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "This welcome belongs to someone else",
                    "Press **🔍 Scan for new members** to start your own.",
                ),
                ephemeral=True,
            )
            return False
        return await require_manage(interaction)

    # ── row 3: channel & message ──────────────────────────────
    @discord.ui.button(label="Edit message", style=discord.ButtonStyle.primary, emoji="✏️", row=3)
    async def edit_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(DraftMessageModal(self.draft))

    @discord.ui.button(label="Make default", style=discord.ButtonStyle.secondary, emoji="⭐", row=3)
    async def make_default(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return
        await cog.set_channel(interaction.guild.id, "welcome", self.draft.channel_id)
        self.draft.settings = await cog.settings(interaction.guild.id)
        self.draft.inform(
            f"<#{self.draft.channel_id}> is now the default welcome channel. It "
            "will be pre-selected every time.",
            "success",
        )
        await render_composer(interaction, self.draft)

    @discord.ui.button(label="Save message", style=discord.ButtonStyle.secondary, emoji="💾", row=3)
    async def save_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return
        await cog.save_message(interaction.guild.id, self.draft.message)
        self.draft.settings = await cog.settings(interaction.guild.id)
        self.draft.message = self.draft.settings.message
        self.draft.inform("Saved as the default welcome message.", "success")
        await render_composer(interaction, self.draft)

    @discord.ui.button(label="Undo edits", style=discord.ButtonStyle.secondary, emoji="↩️", row=3)
    async def revert_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.draft.message = self.draft.settings.message
        self.draft.inform("Back to the default message.", "info")
        await render_composer(interaction, self.draft)

    # ── row 4: send ───────────────────────────────────────────
    @discord.ui.button(label="Send", style=discord.ButtonStyle.success, emoji="✅", row=4)
    async def send(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        await _execute_delivery(interaction, self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️", row=4)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await replace_message(
            interaction, embed_builder.info_embed("Cancelled", "Nothing was sent."), None
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
        logger.exception("Onboarding composer control failed", exc_info=error)
        self.draft.inform(
            "That did not go through. Nothing was sent — the bot console has "
            "the details.",
            "error",
        )
        try:
            await ack(interaction)
            await render_composer(interaction, self.draft)
        except discord.HTTPException:
            logger.warning("Could not report an onboarding failure", exc_info=True)


# ── Delivery execution ─────────────────────────────────────────────────────
async def _execute_delivery(
    interaction: discord.Interaction, view: WelcomeComposerView
) -> None:
    draft = view.draft
    cog = _onboarding_cog(interaction)
    if cog is None:
        await missing_cog(interaction, "Onboarding")
        return

    members = draft.selected_members
    if not members:
        draft.inform("Pick at least one join day first.", "warning")
        await render_composer(interaction, draft)
        return

    channel = None
    if mode_posts(draft.mode):
        channel = await cog.resolve_channel(interaction.guild, draft.channel_id)
        if channel is None:
            draft.channel_id = None
            draft.inform(
                "That channel no longer exists or the bot cannot see it. Pick "
                "another channel.",
                "error",
            )
            await render_composer(interaction, draft)
            return
        missing = missing_permissions(channel, interaction.guild.me)
        if missing:
            draft.inform(
                f"The bot needs **{', '.join(missing)}** in {channel.mention} "
                "before it can post there.",
                "error",
            )
            await render_composer(interaction, draft)
            return

    # Clearing the controls first means a second click cannot send twice.
    def working(done: int = 0) -> discord.Embed:
        lines = [embed_builder.item(f"Members: **{len(members)}**")]
        if channel is not None:
            lines.append(embed_builder.item(f"Posting in {channel.mention}"))
        if mode_dms(draft.mode):
            lines.append(
                embed_builder.item(f"Warm DMs sent: **{done}/{len(members)}** (1 per second)")
            )
        return embed_builder.info_embed("Sending the welcome...", "\n".join(lines))

    await replace_message(interaction, working(), None)
    view.stop()

    async def progress(done: int, total: int) -> None:
        await interaction.edit_original_response(embeds=[working(done)], view=None)

    try:
        result = await cog.deliver(
            guild=interaction.guild,
            members=members,
            message=draft.message,
            moderator=interaction.user,
            channel=channel,
            mode=draft.mode,
            channels=draft.settings.channels,
            progress=progress,
        )
    except discord.Forbidden:
        await _finish(
            interaction,
            embed_builder.error_embed(
                "Missing permissions",
                "The bot could not post in that channel. Grant it **View Channel** "
                "and **Send Messages**, then scan again. Nobody was logged.",
            ),
        )
        return
    except discord.HTTPException as exc:
        logger.exception("Onboarding delivery failed")
        await _finish(
            interaction,
            embed_builder.error_embed(
                "Delivery failed", f"Discord rejected the welcome: {exc}"
            ),
        )
        return

    lines = [
        embed_builder.item(f"Members logged as welcomed: **{result.marked}**"),
        embed_builder.item(
            f"Public post: {result.posted_channel.mention} · [jump to it]({result.jump_url})"
            if result.posted_channel
            else "Public post: *skipped*"
        ),
    ]
    if mode_dms(draft.mode):
        lines.append(
            embed_builder.item(
                f"Warm DMs delivered: **{result.dm_success}** · "
                f"closed DMs: **{result.dm_failed}**"
            )
        )
        if result.failed_names:
            lines.append(
                embed_builder.item("Could not DM: " + ", ".join(result.failed_names))
            )
        if not mode_posts(draft.mode) and result.dm_failed:
            lines.append(
                embed_builder.item(
                    "Members with closed DMs were **not** logged, so they show up "
                    "in the next scan — welcome them with a public post."
                )
            )
    await _finish(interaction, embed_builder.success_embed("Welcome sent", "\n".join(lines)))


async def _finish(interaction: discord.Interaction, embed: discord.Embed) -> None:
    try:
        await interaction.edit_original_response(embeds=[embed], view=None)
    except discord.HTTPException:
        # Very large DM batches can outlive the 15-minute interaction token;
        # the welcome itself already went out, so this summary is best-effort.
        logger.info("Welcome finished after the interaction token expired.")


# ── Settings ───────────────────────────────────────────────────────────────
def build_settings_embed(settings: OnboardingSettings, notice: str | None = None) -> discord.Embed:
    channel_lines = []
    for key, label in CHANNEL_LABELS.items():
        channel_lines.append(
            embed_builder.item(
                f"**{label}:** {_channel_line(settings.channels.get(key), settings.sources[key])}"
            )
            + f"\n*{CHANNEL_HINTS[key]}*"
        )
    snippet = settings.message.replace("\n", " ")
    if len(snippet) > 180:
        snippet = snippet[:180] + "…"
    blocks = []
    if notice:
        blocks += [f"✅ {notice}", embed_builder.SEPARATOR]
    blocks += [
        embed_builder.section("Channels"),
        "\n".join(channel_lines),
        "",
        embed_builder.section("Default welcome message")
        + (" *(yours)*" if settings.custom_message else " *(built-in)*"),
        f"> {snippet}",
        embed_builder.SEPARATOR,
        "Pick a channel in a menu to change it; clear a menu to unset it. "
        "Everything saves instantly and survives restarts.",
        "",
        embed_builder.section("Placeholders"),
        f"`{PLACEHOLDER_HELP}`",
    ]
    return embed_builder.base_embed(
        title="⚙️  Onboarding settings", description="\n".join(blocks)
    )


class SettingsChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, key: str, row: int, settings: OnboardingSettings):
        current = settings.channels.get(key)
        super().__init__(
            placeholder=f"{CHANNEL_LABELS[key]} — not set",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=0,
            max_values=1,
            default_values=[discord.Object(current)] if current else [],
            row=row,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return
        chosen = self.values[0].id if self.values else None
        await cog.set_channel(interaction.guild.id, self.key, chosen)
        label = CHANNEL_LABELS[self.key]
        notice = f"{label} set to <#{chosen}>." if chosen else f"{label} cleared."
        await _render_settings(interaction, notice)


class DefaultMessageModal(discord.ui.Modal, title="Default welcome message"):
    def __init__(self, current: str):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.body = discord.ui.TextInput(
            label="Used as the starting draft for every welcome",
            style=discord.TextStyle.paragraph,
            default=current[:MAX_BODY_LENGTH],
            max_length=MAX_BODY_LENGTH,
            required=True,
            placeholder=PLACEHOLDER_HINT,
        )
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        await ack(interaction)
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return
        await cog.save_message(interaction.guild.id, self.body.value)
        await _render_settings(interaction, "Default welcome message saved.")


class OnboardingSettingsView(discord.ui.View):
    def __init__(self, settings: OnboardingSettings):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.settings = settings
        for row, key in enumerate(CHANNEL_LABELS):
            self.add_item(SettingsChannelSelect(key, row, settings))
        self.reset_message.disabled = not settings.custom_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    @discord.ui.button(label="Edit default message", style=discord.ButtonStyle.primary, emoji="✏️", row=3)
    async def edit_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(DefaultMessageModal(self.settings.message))

    @discord.ui.button(label="Use built-in message", style=discord.ButtonStyle.secondary, emoji="↩️", row=3)
    async def reset_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return
        await cog.save_message(interaction.guild.id, None)
        await _render_settings(interaction, "Restored the built-in welcome message.")

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅", row=3)
    async def done(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await replace_message(
            interaction,
            embed_builder.success_embed(
                "Settings saved", "Your onboarding settings are ready to use."
            ),
            None,
        )
        self.stop()


async def _render_settings(interaction: discord.Interaction, notice: str | None = None) -> None:
    settings = await _onboarding_cog(interaction).settings(interaction.guild.id)
    await replace_message(
        interaction, build_settings_embed(settings, notice), OnboardingSettingsView(settings)
    )


# ── Reset confirmation ─────────────────────────────────────────────────────
class OnboardingResetView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)

    @discord.ui.button(
        label="Yes, clear the onboarding log",
        style=discord.ButtonStyle.danger,
        emoji="♻️",
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        await ack(interaction)
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return
        cleared = await cog.clear_onboarded(interaction.guild.id)
        await replace_message(
            interaction,
            embed_builder.success_embed(
                "Onboarding log cleared",
                embed_builder.item(f"Removed **{cleared}** logged member(s).")
                + "\nEveryone who joined recently can be welcomed again.",
            ),
            None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await replace_message(
            interaction,
            embed_builder.info_embed("Cancelled", "The log was left untouched."),
            None,
        )
        self.stop()


# ── Post the questionnaire ─────────────────────────────────────────────────
MAX_POSTED_LINES = 10


def _questionnaire_missing_permissions(channel: discord.abc.GuildChannel, member: discord.Member) -> list[str]:
    """What the bot lacks to post the questionnaire (an embed) in `channel`."""
    missing = missing_permissions(channel, member)
    if not getattr(channel.permissions_for(member), "embed_links", False):
        missing.append("Embed Links")
    return missing


def build_questionnaire_post_embed(
    channel_id: int | None,
    posted: list[tuple[str, str]] | None = None,
    notice: str | None = None,
) -> discord.Embed:
    lines = []
    if notice:
        lines += [notice, embed_builder.SEPARATOR]
    lines += [
        "Posts the member questionnaire with its **Start onboarding** button. "
        "Post it in as many channels as you like, and as many times as you "
        "like — every copy works, survives restarts and saves answers the same way.",
        "",
        embed_builder.item(
            f"Destination: <#{channel_id}>" if channel_id else "Destination: *pick a channel below*"
        ),
    ]
    if posted:
        lines += ["", embed_builder.section("Posted this session") + f" **{len(posted)}**"]
        lines += [
            embed_builder.item(f"{mention} · [jump to it]({url})")
            for mention, url in posted[-MAX_POSTED_LINES:]
        ]
    return embed_builder.base_embed(title="📝  Post the questionnaire", description="\n".join(lines))


class QuestionnaireChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, channel_id: int | None):
        super().__init__(
            placeholder="Choose the channel to post the questionnaire in...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            default_values=[discord.Object(channel_id)] if channel_id else [],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: QuestionnairePostView = self.view  # type: ignore[assignment]
        channel_id = self.values[0].id
        await replace_message(
            interaction,
            build_questionnaire_post_embed(channel_id, view.posted),
            QuestionnairePostView(channel_id, view.posted),
        )


class QuestionnairePostView(discord.ui.View):
    """Picks a channel and posts a fresh questionnaire there, repeatably."""

    def __init__(
        self, channel_id: int | None = None, posted: list[tuple[str, str]] | None = None
    ):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.channel_id = channel_id
        self.posted = posted if posted is not None else []
        self.add_item(QuestionnaireChannelSelect(channel_id))
        self.post.disabled = not channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    async def _show(self, interaction: discord.Interaction, channel_id: int | None, notice: str) -> None:
        await replace_message(
            interaction,
            build_questionnaire_post_embed(channel_id, self.posted, notice),
            QuestionnairePostView(channel_id, self.posted),
        )

    @discord.ui.button(label="Post questionnaire", style=discord.ButtonStyle.success, emoji="📝", row=1)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await ack(interaction)
        cog = interaction.client.get_cog("QuestionnaireCog")
        if cog is None:
            await missing_cog(interaction, "Questionnaire")
            return
        channel = await resolve_post_channel(interaction.guild, self.channel_id)
        if channel is None:
            await self._show(interaction, None, "❌ That channel could not be opened. Pick another.")
            return
        missing = _questionnaire_missing_permissions(channel, interaction.guild.me)
        if missing:
            await self._show(
                interaction,
                channel.id,
                f"❌ The bot needs **{', '.join(missing)}** in {channel.mention}.",
            )
            return
        try:
            message = await cog.post_copy(channel)
        except discord.HTTPException as exc:
            logger.warning("Posting the questionnaire failed: %s", exc)
            await self._show(interaction, channel.id, f"❌ Discord rejected the questionnaire: {exc}")
            return
        self.posted.append((channel.mention, message.jump_url))
        notice = (
            f"✅ Questionnaire posted in {channel.mention}. Press **📝 Post questionnaire** "
            "again for another copy, or pick a different channel."
        )
        if not cog.configured:
            notice += "\n⚠️ Slack is not connected yet; answers are saved locally and queued."
        await self._show(interaction, channel.id, notice)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, emoji="✖️", row=1)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        count = len(self.posted)
        await replace_message(
            interaction,
            embed_builder.info_embed(
                "Finished",
                f"Posted **{count}** questionnaire{'s' if count != 1 else ''}."
                if count
                else "Nothing was posted.",
            ),
            None,
        )
        self.stop()


async def open_questionnaire_poster(interaction: discord.Interaction) -> None:
    """Privately opens the picker; the moderator chooses where it is posted."""
    await interaction.response.send_message(
        embed=build_questionnaire_post_embed(None),
        view=QuestionnairePostView(None),
        ephemeral=True,
    )


# ── Persistent onboarding hub ──────────────────────────────────────────────
async def build_hub_embed(interaction: discord.Interaction) -> discord.Embed:
    cog = _onboarding_cog(interaction)
    if cog is None:
        return embed_builder.error_embed(
            "Onboarding unavailable", "The onboarding module did not load."
        )
    stats = await cog.onboarding_stats(interaction.guild)
    settings = await cog.settings(interaction.guild.id)

    status_lines = [
        embed_builder.item("Waiting to be welcomed: **{}**".format(stats["pending"])),
        embed_builder.item(
            "Welcomed in the last 7 days: **{}**".format(stats["last_7_days"])
        ),
        embed_builder.item("Welcomed all-time: **{}**".format(stats["total"])),
    ]
    setup_lines = [
        embed_builder.item(
            f"**{label}:** {_channel_line(settings.channels.get(key), settings.sources[key])}"
        )
        for key, label in CHANNEL_LABELS.items()
    ]
    setup_lines.append(
        embed_builder.item(
            "**Welcome message:** "
            + ("your saved message" if settings.custom_message else "built-in")
        )
    )
    how_lines = [
        embed_builder.item(
            "**Scan** finds everyone who joined in the last "
            f"{ONBOARDING_LOOKBACK_DAYS} days and was never welcomed."
        ),
        embed_builder.item(
            "Pick the join days and **the channel to post in** — your default "
            "is pre-selected — then press **Send**."
        ),
        embed_builder.item("**Settings** sets the default channel and message."),
        embed_builder.item(
            "**Post questionnaire** posts the member questionnaire in any channel — "
            "as many copies as you need."
        ),
    ]

    blocks = [
        f"Welcome new members to **{interaction.guild.name}** with a tagged public "
        "post and a warm, branded DM — then keep a log so nobody is welcomed twice.",
        embed_builder.SEPARATOR,
        embed_builder.section("Right now"),
        "\n".join(status_lines),
        "",
        embed_builder.section("Setup"),
        "\n".join(setup_lines),
        "",
        embed_builder.section("How to use it"),
        "\n".join(how_lines),
    ]
    return embed_builder.base_embed(
        title="ℹ️  Information Onboarding",
        description="\n".join(blocks),
    )


class OnboardingHubView(discord.ui.View):
    """Persistent onboarding hub (static custom_ids, survives restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Post questionnaire", style=discord.ButtonStyle.primary,
                       custom_id="saferest_onboarding:questionnaire", row=2)
    async def post_questionnaire(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Opens the picker that posts questionnaire copies to any channel."""
        if not await require_manage(interaction):
            return
        await open_questionnaire_poster(interaction)

    @discord.ui.button(
        label="Scan for new members",
        style=discord.ButtonStyle.success,
        custom_id="saferest_onboarding:scan",
        emoji="🔍",
        row=0,
    )
    async def scan(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        pending = await cog.pending_members(interaction.guild)
        if not pending:
            await interaction.followup.send(
                embed=embed_builder.success_embed(
                    "Everyone is welcomed",
                    f"No member who joined in the last {ONBOARDING_LOOKBACK_DAYS} "
                    "days is waiting for a welcome.",
                ),
                ephemeral=True,
            )
            return

        settings = await cog.settings(interaction.guild.id)
        draft = WelcomeDraft.start(interaction.user.id, cog.group_by_day(pending), settings)
        if draft.channel_id:
            channel = await cog.resolve_channel(interaction.guild, draft.channel_id)
            if channel is None:
                draft.channel_id = None
                draft.inform(
                    "Your default welcome channel no longer exists. Pick a channel "
                    "below, then press **⭐ Make default**.",
                    "warning",
                )
        else:
            draft.inform(
                "No default welcome channel yet. Pick one below and press "
                "**⭐ Make default** to remember it.",
                "info",
            )

        await interaction.followup.send(
            embeds=build_composer_embeds(draft, interaction.guild),
            view=WelcomeComposerView(draft),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Settings",
        style=discord.ButtonStyle.primary,
        custom_id="saferest_onboarding:setup",
        emoji="⚙️",
        row=0,
    )
    async def setup_channels(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        cog = _onboarding_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Onboarding")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await cog.settings(interaction.guild.id)
        await interaction.followup.send(
            embed=build_settings_embed(settings),
            view=OnboardingSettingsView(settings),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Onboarding stats",
        style=discord.ButtonStyle.secondary,
        custom_id="saferest_onboarding:stats",
        emoji="📋",
        row=1,
    )
    async def stats(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            embed=await build_hub_embed(interaction), ephemeral=True
        )

    @discord.ui.button(
        label="Reset onboarding log",
        style=discord.ButtonStyle.danger,
        custom_id="saferest_onboarding:reset",
        emoji="♻️",
        row=1,
    )
    async def reset(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        await interaction.response.send_message(
            embed=embed_builder.warning_embed(
                "Clear the onboarding log?",
                "Every member who joined recently becomes selectable again, so they "
                "can receive a second welcome. This cannot be undone.",
            ),
            view=OnboardingResetView(),
            ephemeral=True,
        )


__all__ = [
    "OnboardingHubView",
    "OnboardingSettingsView",
    "QuestionnairePostView",
    "WelcomeComposerView",
    "WelcomeDraft",
    "build_composer_embeds",
    "build_hub_embed",
    "build_questionnaire_post_embed",
    "build_settings_embed",
    "open_questionnaire_poster",
]
