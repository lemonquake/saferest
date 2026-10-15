"""
SafeRest Discord Announcement screens for the Mod Panel.
Created by Aljay Leodones for SafeRest.

One hub lists the four recurring weekly posts; each has its own screen where a
moderator can read the current copy, edit it, swap the banner, publish it to a
channel straight away or hand it to the scheduler. Every edit is written back
to the announcement's saved template, so the Embed Studio and this screen are
always looking at the same message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import discord

from config import BRAND_NAME, REPORT_TIMEZONE
from core.announcements import (
    ANNOUNCEMENTS,
    WEEKDAY_NAMES,
    Announcement,
    AnnouncementSchedule,
    get_announcement,
    get_announcement_store,
    parse_time,
    parse_weekday,
)
from core.embed_script import EmbedScript
from core.embed_templates import TemplateError
from utils import embed_builder
from views.banner import BannerModal, describe_banner
from views.broadcast_view import (
    TEXT_CHANNEL_TYPES,
    EmbedComposerModal,
    channel_context,
    show_preview,
)
from views.common import ack, replace_message, require_manage

logger = logging.getLogger("saferest_bot.announcement_ui")

STORAGE_HINT = (
    "The announcement library could not be reached. The bot console has the "
    "details — try again in a moment."
)


def _shorten(text: str | None, limit: int, *, fallback: str = "*not set*") -> str:
    if not text:
        return fallback
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _yes_no(raw: str | None, *, default: bool) -> bool:
    """Reads the `yes / no` answer from a modal, keeping `default` when blank."""
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text in {"y", "yes", "on", "true", "1"}:
        return True
    if text in {"n", "no", "off", "false", "0"}:
        return False
    raise ValueError(f"`{raw}` is not an answer — type `yes` or `no`.")


# ── Hub ─────────────────────────────────────────────────────────────────────
async def build_announcement_hub_embed(
    interaction: discord.Interaction,
) -> discord.Embed:
    """The overview card: what each weekly post is and when it goes out."""
    embed = embed_builder.base_embed(
        title=f"{BRAND_NAME} — Discord Announcements",
        description=(
            f"{embed_builder.SEPARATOR}\n"
            "The four recurring posts of the week. Open one to read it, edit "
            "the copy, change the banner, publish it now or put it on a "
            "schedule.\n"
            f"Scheduled times use the **{REPORT_TIMEZONE}** timezone."
        ),
    )

    schedules: dict[str, AnnouncementSchedule] = {}
    if interaction.guild is not None:
        try:
            schedules = await get_announcement_store().list_schedules(
                interaction.guild.id
            )
        except Exception:  # noqa: BLE001 - the hub must still open
            logger.exception("Could not read announcement schedules")

    for announcement in ANNOUNCEMENTS:
        schedule = schedules.get(announcement.slug)
        embed.add_field(
            name=embed_builder.section(
                f"{announcement.emoji}  {announcement.label}"
            ),
            value="\n".join(
                [
                    embed_builder.item(announcement.summary),
                    embed_builder.item(
                        schedule.describe()
                        if schedule
                        else "Not scheduled yet — posts only when you send it."
                    ),
                ]
            ),
            inline=False,
        )

    embed.add_field(
        name=embed_builder.section("Where the copy lives"),
        value=embed_builder.item(
            "Each post is saved in the template library as "
            "**SafeRest Announcement 1-4**, so it can also be opened from "
            "**Embed Message → Manage templates**."
        ),
        inline=False,
    )
    return embed


async def open_announcements(interaction: discord.Interaction) -> None:
    """Sends the announcement hub as a private panel."""
    if interaction.guild is not None:
        try:
            await get_announcement_store().seed_all(
                interaction.guild.id, actor_id=interaction.user.id
            )
        except Exception:  # noqa: BLE001 - a seeding failure must not block the hub
            logger.exception("Could not seed the announcement templates")

    embed = await build_announcement_hub_embed(interaction)
    view = AnnouncementHubView()
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )


class AnnouncementHubView(discord.ui.View):
    """
    Persistent hub for the four weekly posts.

    Static `custom_id`s mean a hub posted with `/announcements` keeps working
    after a restart; each button opens a fresh private screen, so the view
    itself never has to remember anything.
    """

    def __init__(self):
        super().__init__(timeout=None)
        for index, announcement in enumerate(ANNOUNCEMENTS):
            self.add_item(AnnouncementHubButton(announcement, row=index // 2))


class AnnouncementHubButton(discord.ui.Button):
    def __init__(self, announcement: Announcement, row: int):
        super().__init__(
            label=announcement.label,
            style=discord.ButtonStyle.primary,
            emoji=announcement.emoji,
            custom_id=f"saferest_announce:{announcement.slug}",
            row=row,
        )
        self.slug = announcement.slug

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_announcement(interaction, get_announcement(self.slug))


# ── Per-announcement screen ─────────────────────────────────────────────────
@dataclass(slots=True)
class ScreenState:
    """Everything one announcement screen is currently showing."""

    guild_id: int
    user_id: int
    announcement: Announcement
    script: EmbedScript
    schedule: AnnouncementSchedule | None = None
    # Applies to **Post now** only; the schedule keeps its own ping setting.
    ping_now: bool = False
    notice: str | None = None
    notice_level: str = "info"
    _channels: dict[str, int | None] = field(default_factory=dict)

    @property
    def store(self):
        return get_announcement_store()

    def inform(self, message: str, level: str = "info") -> None:
        self.notice = message
        self.notice_level = level

    def take_notice(self) -> tuple[str | None, str]:
        message, level = self.notice, self.notice_level
        self.notice, self.notice_level = None, "info"
        return message, level

    async def reload(self) -> None:
        """Re-reads the copy and the schedule after any change."""
        self.script = await self.store.script(
            self.guild_id, self.announcement, user_id=self.user_id
        )
        self.schedule = await self.store.get_schedule(
            self.guild_id, self.announcement.slug
        )


def build_screen_embeds(
    state: ScreenState, guild: discord.Guild | None
) -> list[discord.Embed]:
    """The control card plus a live render of the post itself."""
    announcement = state.announcement
    script = state.script

    card = embed_builder.base_embed(
        title=f"{announcement.emoji}  {announcement.label}",
        description=(
            f"{embed_builder.SEPARATOR}\n{announcement.summary}\n"
            "The preview below is exactly what members will see."
        ),
    )

    card.add_field(
        name=embed_builder.section("Saved as"),
        value=embed_builder.item(
            f"**{script.template_name or announcement.template_name}**"
        ),
        inline=False,
    )
    card.add_field(
        name=embed_builder.section("Text above the embed"),
        value=embed_builder.item(
            _shorten(script.content, 120, fallback="*none — the embed posts alone*")
        ),
        inline=False,
    )
    card.add_field(
        name=embed_builder.section("Banner image"),
        value=embed_builder.item(
            describe_banner(
                script, missing="*none — add one with* **Banner image**"
            )
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
        name=embed_builder.section("Schedule"),
        value=embed_builder.item(
            state.schedule.describe()
            if state.schedule
            else "Not scheduled — use **Schedule** to post it automatically."
        ),
        inline=False,
    )
    card.add_field(
        name=embed_builder.section("Post now will"),
        value=embed_builder.item(
            "**ping @everyone**" if state.ping_now else "post without pinging anyone"
        ),
        inline=False,
    )

    notice, level = state.take_notice()
    embeds = [card]
    if notice:
        builder = {
            "success": embed_builder.success_embed,
            "error": embed_builder.error_embed,
            "warning": embed_builder.warning_embed,
        }.get(level, embed_builder.info_embed)
        embeds.append(builder("Update", notice))

    preview = script.render_embed(guild=guild, channels=state._channels)
    if preview is not None:
        embeds.append(preview)
    return embeds


async def render_screen(interaction: discord.Interaction, state: ScreenState) -> None:
    await replace_message(
        interaction,
        build_screen_embeds(state, interaction.guild),
        AnnouncementScreenView(state),
        # An uploaded banner rides along with every redraw: the preview embed
        # points at `attachment://…`, which only resolves while the file is
        # on the message.
        attachments=state.script.render_files(),
    )


async def open_announcement(
    interaction: discord.Interaction, announcement: Announcement
) -> None:
    """Loads one announcement and sends its screen as a private panel."""
    if interaction.guild is None:
        return
    store = get_announcement_store()
    try:
        script = await store.script(
            interaction.guild.id, announcement, user_id=interaction.user.id
        )
        schedule = await store.get_schedule(interaction.guild.id, announcement.slug)
    except TemplateError as exc:
        await interaction.followup.send(
            embed=embed_builder.error_embed("Announcement unavailable", str(exc)),
            ephemeral=True,
        )
        return
    except Exception:  # noqa: BLE001
        logger.exception("Could not open announcement %s", announcement.slug)
        await interaction.followup.send(
            embed=embed_builder.error_embed("Announcement unavailable", STORAGE_HINT),
            ephemeral=True,
        )
        return

    state = ScreenState(
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
        announcement=announcement,
        script=script,
        schedule=schedule,
        # Monday ships with an `@everyone` line, so its ping starts on; the
        # screen always says so before anything is published.
        ping_now=(
            schedule.mention_everyone
            if schedule
            else "@everyone" in (announcement.payload.get("content") or "")
        ),
    )
    state._channels = await channel_context(interaction)

    await interaction.followup.send(
        embeds=build_screen_embeds(state, interaction.guild),
        view=AnnouncementScreenView(state),
        files=state.script.render_files(),
        ephemeral=True,
    )


class AnnouncementScreenView(discord.ui.View):
    """Controls for one weekly post."""

    def __init__(self, state: ScreenState):
        super().__init__(timeout=900)
        self.state = state
        self.icon_toggle.label = (
            "Server icon: ON" if state.script.show_icon else "Server icon: OFF"
        )
        # A picture always posts without the icon, so the toggle has nothing to do.
        self.icon_toggle.disabled = state.script.has_image
        self.ping_toggle.label = (
            "Ping @everyone: ON" if state.ping_now else "Ping @everyone: OFF"
        )
        self.ping_toggle.style = (
            discord.ButtonStyle.danger if state.ping_now else discord.ButtonStyle.secondary
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "This panel belongs to someone else",
                    "Open your own from the Mod Panel — **SafeRest Discord "
                    "Announcement**.",
                ),
                ephemeral=True,
            )
            return False
        return await require_manage(interaction)

    # ── row 0: publishing and copy ────────────────────────────
    @discord.ui.button(label="Post now", style=discord.ButtonStyle.success, emoji="📣", row=0)
    async def post_now(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Opens the preview with the channel picker attached."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await show_preview(
            interaction,
            self.state.script,
            mention_everyone=self.state.ping_now,
            intro_title=f"{self.state.announcement.label} — ready to post",
        )

    @discord.ui.button(label="Edit message", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def edit_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state

        async def applied(
            modal_interaction: discord.Interaction, notes: list[str]
        ) -> None:
            await ack(modal_interaction)
            await _persist(modal_interaction, state, notes=notes)

        # The modal edits `state.script` in place, so `_persist` writes exactly
        # what the moderator just typed back to the template.
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
            if level in {"error", "warning", "info"}:
                # Nothing reached the draft, so there is nothing to write back.
                state.inform(notice, level)
                await render_screen(modal_interaction, state)
                return
            await _persist(modal_interaction, state, success=notice)

        await interaction.response.send_modal(
            BannerModal(state.script, on_apply=applied)
        )

    @discord.ui.button(label="Server icon: ON", style=discord.ButtonStyle.secondary, emoji="🏷️", row=0)
    async def icon_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Shows or hides the server icon, and saves the choice."""
        await ack(interaction)
        state = self.state
        state.script.show_icon = not state.script.show_icon
        await _persist(
            interaction,
            state,
            success=(
                "The server icon will show on this post."
                if state.script.show_icon
                else "The server icon is hidden — the banner carries the "
                "branding on its own."
            ),
        )

    # ── row 1: schedule and safety ────────────────────────────
    @discord.ui.button(label="Schedule", style=discord.ButtonStyle.primary, emoji="🗓️", row=1)
    async def schedule(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        await replace_message(
            interaction,
            build_schedule_embed(self.state),
            AnnouncementScheduleView(self.state),
            attachments=[],
        )

    @discord.ui.button(label="Ping @everyone: OFF", style=discord.ButtonStyle.secondary, row=1)
    async def ping_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        self.state.ping_now = not self.state.ping_now
        self.state.inform(
            "**Post now** will ping @everyone."
            if self.state.ping_now
            else "**Post now** will not ping anyone.",
            "warning" if self.state.ping_now else "info",
        )
        await render_screen(interaction, self.state)

    @discord.ui.button(label="Reset to default", style=discord.ButtonStyle.danger, emoji="↩️", row=1)
    async def reset(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        await replace_message(
            interaction,
            embed_builder.warning_embed(
                "Restore the original copy?",
                f"**{self.state.announcement.label}** will go back to the "
                "wording, banner and colour it shipped with. Any edits your "
                "team saved are overwritten and cannot be recovered.",
            ),
            ResetConfirmView(self.state),
            attachments=[],
        )

    # ── row 2: navigation ─────────────────────────────────────
    @discord.ui.button(label="Back to announcements", style=discord.ButtonStyle.secondary, emoji="◀️", row=2)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        await replace_message(
            interaction,
            await build_announcement_hub_embed(interaction),
            AnnouncementHubView(),
            attachments=[],
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Announcement control failed", exc_info=error)
        self.state.inform(
            "That did not go through. The bot console has the details.", "error"
        )
        try:
            await render_screen(interaction, self.state)
        except discord.HTTPException:
            logger.warning("Could not report an announcement failure", exc_info=True)


async def _persist(
    interaction: discord.Interaction,
    state: ScreenState,
    *,
    notes: list[str] | None = None,
    success: str | None = None,
) -> None:
    """Saves the edited draft back to the template and redraws the screen."""
    try:
        await state.store.save(
            guild_id=state.guild_id,
            announcement=state.announcement,
            script=state.script,
            actor_id=state.user_id,
        )
    except TemplateError as exc:
        state.inform(str(exc), "error")
    except Exception:  # noqa: BLE001
        logger.exception("Could not save announcement %s", state.announcement.slug)
        state.inform(STORAGE_HINT, "error")
    else:
        lines = [success or "Saved. Every future post uses this version."]
        lines.extend(notes or [])
        state.inform("\n".join(lines), "success")

    await state.reload()
    await render_screen(interaction, state)


class ResetConfirmView(discord.ui.View):
    def __init__(self, state: ScreenState):
        super().__init__(timeout=120)
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.state.user_id

    @discord.ui.button(label="Yes, restore it", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        state = self.state
        try:
            await state.store.reset(
                guild_id=state.guild_id,
                announcement=state.announcement,
                actor_id=state.user_id,
            )
        except TemplateError as exc:
            state.inform(str(exc), "error")
        except Exception:  # noqa: BLE001
            logger.exception("Could not reset announcement %s", state.announcement.slug)
            state.inform(STORAGE_HINT, "error")
        else:
            state.inform("The original copy and banner are back.", "success")
        await state.reload()
        await render_screen(interaction, state)
        self.stop()

    @discord.ui.button(label="Keep our version", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        self.state.inform("Nothing was changed.")
        await render_screen(interaction, self.state)
        self.stop()


# ── Scheduling ──────────────────────────────────────────────────────────────
def build_schedule_embed(state: ScreenState) -> discord.Embed:
    announcement = state.announcement
    schedule = state.schedule

    embed = embed_builder.base_embed(
        title=f"🗓️  Schedule — {announcement.label}",
        description=(
            f"{embed_builder.SEPARATOR}\n"
            "Pick the channel first, then set the day and time. The bot posts "
            "it every week on its own.\n"
            f"All times are **{REPORT_TIMEZONE}**."
        ),
    )
    if schedule is None:
        embed.add_field(
            name=embed_builder.section("Current schedule"),
            value=embed_builder.item(
                "None yet. Choosing a channel below starts one at "
                f"**{announcement.day} {announcement.default_time}**, which "
                "you can change afterwards."
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name=embed_builder.section("Current schedule"),
            value="\n".join(
                [
                    embed_builder.item(f"Channel: <#{schedule.channel_id}>"),
                    embed_builder.item(f"Day: **{schedule.day}**"),
                    embed_builder.item(f"Time: **{schedule.clock}**"),
                    embed_builder.item(
                        "Pings @everyone: "
                        + ("**yes**" if schedule.mention_everyone else "no")
                    ),
                    embed_builder.item(
                        "Status: " + ("**active**" if schedule.enabled else "**paused**")
                    ),
                    embed_builder.item(
                        f"Last posted: **{schedule.last_run or 'never'}**"
                    ),
                ]
            ),
            inline=False,
        )

    notice, level = state.take_notice()
    if notice:
        embed.add_field(
            name=embed_builder.section("Update"), value=notice, inline=False
        )
    return embed


class ScheduleChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, state: ScreenState):
        super().__init__(
            placeholder="Choose the channel this post goes to...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            row=0,
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        await ack(interaction)
        state = self.state
        schedule = state.schedule
        announcement = state.announcement
        try:
            await state.store.set_schedule(
                guild_id=state.guild_id,
                slug=announcement.slug,
                channel_id=self.values[0].id,
                weekday=schedule.weekday if schedule else announcement.weekday,
                hour=schedule.hour if schedule else announcement.default_hour,
                minute=schedule.minute if schedule else announcement.default_minute,
                mention_everyone=(
                    schedule.mention_everyone if schedule else state.ping_now
                ),
                enabled=schedule.enabled if schedule else True,
                actor_id=state.user_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not save the schedule channel")
            state.inform(STORAGE_HINT, "error")
        else:
            state.inform(f"Channel set to <#{self.values[0].id}>.", "success")
        await _render_schedule(interaction, state)


async def _render_schedule(
    interaction: discord.Interaction, state: ScreenState
) -> None:
    state.schedule = await state.store.get_schedule(
        state.guild_id, state.announcement.slug
    )
    await replace_message(
        interaction,
        build_schedule_embed(state),
        AnnouncementScheduleView(state),
        attachments=[],
    )


class AnnouncementScheduleView(discord.ui.View):
    def __init__(self, state: ScreenState):
        super().__init__(timeout=900)
        self.state = state
        self.add_item(ScheduleChannelSelect(state))

        scheduled = state.schedule is not None
        self.set_time.disabled = not scheduled
        self.remove.disabled = not scheduled
        self.pause.disabled = not scheduled
        if scheduled and not state.schedule.enabled:
            self.pause.label = "Resume"
            self.pause.style = discord.ButtonStyle.success
            self.pause.emoji = "▶️"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "This panel belongs to someone else",
                    "Open your own from the Mod Panel.",
                ),
                ephemeral=True,
            )
            return False
        return await require_manage(interaction)

    @discord.ui.button(label="Set day & time", style=discord.ButtonStyle.primary, emoji="⏰", row=1)
    async def set_time(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ScheduleTimeModal(self.state))

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️", row=1)
    async def pause(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        state = self.state
        schedule = state.schedule
        if schedule is None:
            state.inform("There is no schedule to pause yet.", "error")
        else:
            resume = not schedule.enabled
            await state.store.set_enabled(
                guild_id=state.guild_id, slug=state.announcement.slug, enabled=resume
            )
            state.inform(
                "The schedule is running again."
                if resume
                else "The schedule is paused — nothing posts until you resume it.",
                "success" if resume else "warning",
            )
        await _render_schedule(interaction, state)

    @discord.ui.button(label="Remove schedule", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        state = self.state
        removed = await state.store.clear_schedule(
            guild_id=state.guild_id, slug=state.announcement.slug
        )
        state.inform(
            "The schedule was removed. The post can still be sent manually."
            if removed
            else "There was no schedule to remove.",
            "success" if removed else "info",
        )
        await _render_schedule(interaction, state)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=2)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await ack(interaction)
        await self.state.reload()
        await render_screen(interaction, self.state)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Schedule control failed", exc_info=error)
        self.state.inform(
            "That did not go through. The bot console has the details.", "error"
        )
        try:
            await _render_schedule(interaction, self.state)
        except discord.HTTPException:
            logger.warning("Could not report a schedule failure", exc_info=True)


class ScheduleTimeModal(discord.ui.Modal, title="When should this post?"):
    def __init__(self, state: ScreenState):
        super().__init__()
        self.state = state
        schedule = state.schedule
        announcement = state.announcement

        self.day = discord.ui.TextInput(
            label="Day of the week",
            placeholder="Monday, Tue, Wednesday...",
            default=WEEKDAY_NAMES[
                schedule.weekday if schedule else announcement.weekday
            ],
            required=False,
            max_length=12,
        )
        self.clock = discord.ui.TextInput(
            label=f"Time, 24-hour ({REPORT_TIMEZONE})",
            placeholder="09:00",
            default=schedule.clock if schedule else announcement.default_time,
            required=True,
            max_length=5,
        )
        self.ping = discord.ui.TextInput(
            label="Ping @everyone? (yes / no)",
            default="yes"
            if (schedule.mention_everyone if schedule else state.ping_now)
            else "no",
            required=False,
            max_length=3,
        )
        for item in (self.day, self.clock, self.ping):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        await ack(interaction)
        state = self.state
        schedule = state.schedule
        if schedule is None:
            state.inform(
                "Choose the channel first — the schedule needs somewhere to "
                "post.",
                "error",
            )
            await _render_schedule(interaction, state)
            return

        try:
            weekday = parse_weekday(self.day.value, default=schedule.weekday)
            hour, minute = parse_time(self.clock.value)
            ping = _yes_no(self.ping.value, default=schedule.mention_everyone)
        except ValueError as exc:
            state.inform(str(exc), "error")
            await _render_schedule(interaction, state)
            return

        await state.store.set_schedule(
            guild_id=state.guild_id,
            slug=state.announcement.slug,
            channel_id=schedule.channel_id,
            weekday=weekday,
            hour=hour,
            minute=minute,
            mention_everyone=ping,
            enabled=schedule.enabled,
            actor_id=state.user_id,
        )
        state.inform(
            f"Scheduled for every **{WEEKDAY_NAMES[weekday]} "
            f"{hour:02d}:{minute:02d}**"
            + (" — and it will ping @everyone." if ping else "."),
            "success",
        )
        await _render_schedule(interaction, state)


__all__ = [
    "AnnouncementHubView",
    "build_announcement_hub_embed",
    "open_announcements",
]
