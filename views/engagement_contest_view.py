"""
Moderator UI for running the Engagement Contest.
Created by Aljay Leodones for SafeRest.

The hub is the one place a moderator manages a contest end to end: start a fresh
one (which fixes the starting point of the count), publish the live Top 15
Leaderboard, end it and freeze the final results, browse past contests, or wipe
everything and begin again.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from cogs.engagement_contest import (
    ContestSession,
    ContestSettings,
    describe_session_period,
    render_leaderboard_embed,
    snapshot_channel_ids,
    snapshot_to_standings,
)
from config import (
    CONTEST_MESSAGE_COOLDOWN_SECONDS,
    CONTEST_MIN_MESSAGE_WORDS,
    CONTEST_POINTS_MESSAGE,
    CONTEST_POINTS_REACTION,
    CONTEST_POINTS_TIKTOK,
    CONTEST_SELF_REPEAT_HOURS,
    REPORT_TIMEZONE,
)
from utils import embed_builder
from views.common import ack, replace_message

logger = logging.getLogger("saferest_bot.contest_ui")

TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
# Discord lets one channel picker hold at most 25 channels.
MAX_CONTEST_CHANNELS = 25

SCORING_RULES = (
    f"▸ 💬 **Genuine message — {CONTEST_POINTS_MESSAGE} pts**\n"
    f"     └ must be more than {CONTEST_MIN_MESSAGE_WORDS - 1} real words; "
    f"links, mentions and emoji do not count as words\n"
    f"▸ 🎬 **TikTok video post — {CONTEST_POINTS_TIKTOK} pts**\n"
    f"     └ each clip scores once per member\n"
    f"▸ ❤️ **Reaction — {CONTEST_POINTS_REACTION} pt**\n"
    f"     └ every reaction added in a contest channel\n"
    f"▸ 🚫 **Not counted:** repeated text within {CONTEST_SELF_REPEAT_HOURS}h, "
    f"copy-pasted text, bursts under {CONTEST_MESSAGE_COOLDOWN_SECONDS}s apart\n"
    f"▸ 🛡️ **Excluded:** moderators and bots"
)


def _report_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(REPORT_TIMEZONE)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown REPORT_TIMEZONE %r; falling back to UTC", REPORT_TIMEZONE)
        return ZoneInfo("UTC")


def _can_manage(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


async def _deny_if_unauthorized(interaction: discord.Interaction) -> bool:
    """Replies with the permission notice and returns True when blocked."""
    if _can_manage(interaction):
        return False
    await interaction.response.send_message(
        "You need the **Manage Server** permission to run the Engagement Contest.",
        ephemeral=True,
    )
    return True


def _get_cog(interaction: discord.Interaction):
    return interaction.client.get_cog("EngagementContestCog")


def _local(moment: datetime) -> str:
    return moment.astimezone(_report_timezone()).strftime("%d %b %Y, %I:%M %p")


def get_today_range() -> tuple[datetime, datetime, str]:
    """Calculates range from midnight today (in REPORT_TIMEZONE) to the current moment."""
    tz = _report_timezone()
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    now_utc = datetime.now(timezone.utc)
    start_utc = start_local.astimezone(timezone.utc)
    return (
        start_utc,
        now_utc,
        f"Starting Today ({now_local.strftime('%B %d, %Y')} • {REPORT_TIMEZONE})",
    )


def _parse_custom_contest_range(
    start_text: str, end_text: str
) -> tuple[datetime, datetime]:
    tz = _report_timezone()

    def parse(value: str, *, is_end: bool) -> datetime:
        clean_value = value.strip()
        if not clean_value and is_end:
            return datetime.now(tz)
        try:
            parsed = datetime.strptime(clean_value, "%Y-%m-%d %H:%M")
            return parsed.replace(tzinfo=tz)
        except ValueError:
            pass
        try:
            parsed_date = datetime.strptime(clean_value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(
                "Use YYYY-MM-DD or YYYY-MM-DD HH:MM for both dates."
            ) from exc
        parsed = datetime.combine(parsed_date, time.min, tzinfo=tz)
        return parsed + timedelta(days=1) if is_end else parsed

    start = parse(start_text, is_end=False)
    end = parse(end_text, is_end=True)
    if end <= start:
        raise ValueError("The end date/time must be after the start date/time.")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# -- Hub embed --------------------------------------------------------------


def _channel_mentions(channel_ids) -> str:
    return " ".join(f"<#{cid}>" for cid in sorted(channel_ids))


async def _leaderboard_channel_ids(interaction: discord.Interaction, session: ContestSession):
    """The channels a past contest counted, falling back to today's settings."""
    saved = snapshot_channel_ids(session.snapshot)
    if saved is not None:
        return saved
    return (await _get_cog(interaction).settings(interaction.guild.id)).channel_ids


async def build_contest_hub_embed(interaction: discord.Interaction) -> discord.Embed:
    """The Engagement Contest control panel, showing the live contest status."""
    cog = _get_cog(interaction)
    settings: ContestSettings | None = None
    if cog is not None and interaction.guild is not None:
        settings = await cog.settings(interaction.guild.id)
    destination = (
        f"<#{settings.report_channel_id}>"
        if settings and settings.report_channel_id
        else "the leaderboard channel (pick it in **⚙️ Settings**)"
    )
    embed = embed_builder.base_embed(
        title="🏆  Engagement Contest",
        description=(
            f"{embed_builder.SEPARATOR}\n"
            "Start a contest to fix the starting point of the count. Everything "
            f"members do from that moment on is scored, and the **Top 15 "
            f"Leaderboard** is published to {destination}.\n"
            f"{embed_builder.SEPARATOR}"
        ),
        color=embed_builder.COLOR_ACCENT,
    )

    session: ContestSession | None = None
    if cog is not None and interaction.guild is not None:
        session = await cog.get_active_contest(interaction.guild.id)

    if session is None:
        embed.add_field(
            name=embed_builder.section("Status"),
            value=(
                "🔴 **No contest is running.**\n"
                "Press **Start New Contest** to open one — counting begins the "
                "instant you confirm."
            ),
            inline=False,
        )
    else:
        started_by = session.started_by_name or "a moderator"
        embed.add_field(
            name=embed_builder.section("Status"),
            value=(
                f"🟢 **{session.name}** is running.\n"
                f"{embed_builder.item(f'Started: **{_local(session.started_at)}** ({REPORT_TIMEZONE})')}\n"
                f"{embed_builder.item(f'Running for: **{session.describe_duration()}**')}\n"
                f"{embed_builder.item(f'Opened by: **{started_by}**')}"
            ),
            inline=False,
        )

    embed.add_field(
        name=embed_builder.section("Scoring Rules"),
        value=SCORING_RULES,
        inline=False,
    )
    embed.add_field(
        name=embed_builder.section("Counted Channels"),
        value=(
            _channel_mentions(settings.channel_ids)
            if settings and settings.channel_ids
            else "⚠️ *None yet.* Press **⚙️ Settings** to choose which channels count."
        ),
        inline=False,
    )
    return embed


# -- Shared publishing helper ----------------------------------------------


async def _run_contest_publish(
    interaction: discord.Interaction,
    *,
    start: datetime,
    end: datetime,
    period_label: str,
    session: ContestSession | None = None,
    is_final: bool = False,
) -> None:
    """Scans, scores and publishes a leaderboard, then reports back privately."""
    if interaction.guild is None:
        await interaction.followup.send(
            "Engagement contest reports can only be generated inside a server.",
            ephemeral=True,
        )
        return

    cog = _get_cog(interaction)
    if cog is None:
        await interaction.followup.send(
            "The engagement contest module is not ready. Check bot logs.",
            ephemeral=True,
        )
        return

    try:
        channel, top_rows, scan_stats, filter_stats = await cog.publish_contest_leaderboard(
            guild=interaction.guild,
            start=start,
            end=end,
            period_label=period_label,
            requested_by=interaction.user,
            session=session,
            is_final=is_final,
        )
    except (RuntimeError, discord.Forbidden, discord.HTTPException) as exc:
        logger.exception("Could not publish engagement contest leaderboard")
        await interaction.followup.send(
            f"The contest leaderboard could not be published: {exc}", ephemeral=True
        )
        return
    except Exception:
        logger.exception("Unexpected contest leaderboard failure")
        await interaction.followup.send(
            "The contest scan failed unexpectedly. Check the bot console for details.",
            ephemeral=True,
        )
        return

    first_place = (
        f"🥇 Leader: **{top_rows[0].display_name}** ({top_rows[0].total_points:,} pts)"
        if top_rows
        else "No qualifying participants yet"
    )
    await interaction.followup.send(
        f"✅ **Leaderboard published in {channel.mention}!**\n"
        f"▸ Ranked Top {len(top_rows)} participant(s)\n"
        f"▸ {first_place}\n"
        f"▸ Inspected {scan_stats.messages_scanned:,} messages across "
        f"{scan_stats.channels_scanned:,} contest channel(s)/thread(s)\n"
        f"▸ Filtered {filter_stats.total_filtered:,} spam / duplicate / too-short "
        f"item(s) out of the scoring",
        ephemeral=True,
    )


# -- Start a new contest ----------------------------------------------------


class StartContestModal(discord.ui.Modal, title="Start a New Engagement Contest"):
    contest_name = discord.ui.TextInput(
        label="Contest name",
        placeholder="e.g. September Engagement Contest",
        min_length=3,
        max_length=80,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _deny_if_unauthorized(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Contests can only be started inside a server.", ephemeral=True
            )
            return

        cog = _get_cog(interaction)
        if cog is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready. Check bot logs.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        previous = await cog.get_active_contest(interaction.guild.id)
        session = await cog.start_contest(
            guild=interaction.guild,
            name=self.contest_name.value.strip(),
            started_by=interaction.user,
        )

        note = (
            f"\n▸ ⚠️ **{previous.name}** was still running and has been closed."
            if previous is not None
            else ""
        )
        await interaction.followup.send(
            embed=embed_builder.success_embed(
                f"Contest started — {session.name}",
                (
                    f"The count starts **now**. Anything posted before this moment "
                    f"is not part of this contest.\n\n"
                    f"{embed_builder.item(f'Started: **{_local(session.started_at)}** ({REPORT_TIMEZONE})')}\n"
                    f"{embed_builder.item(f'Contest ID: **#{session.id}**')}\n"
                    f"{embed_builder.item('Publish the standings any time with **Publish Leaderboard**.')}"
                    f"{note}"
                ),
            ),
            ephemeral=True,
        )


# -- Confirmations ----------------------------------------------------------


class ConfirmActionView(discord.ui.View):
    """Two-step guard for the destructive contest actions."""

    def __init__(
        self,
        *,
        confirm_label: str,
        style: discord.ButtonStyle,
        parent: discord.Interaction | None = None,
    ):
        super().__init__(timeout=60)
        self.confirmed: bool | None = None
        self.parent = parent
        self.confirm_button.label = confirm_label
        self.confirm_button.style = style

    async def on_timeout(self) -> None:
        """Nothing was confirmed in time, so the prompt retires itself."""
        if self.parent is None:
            return
        try:
            await self.parent.edit_original_response(
                embed=embed_builder.info_embed(
                    "Timed out", "Nothing was changed. Run the action again if you meant to."
                ),
                view=None,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        self.stop()
        await interaction.response.edit_message(
            embed=embed_builder.info_embed("Cancelled", "Nothing was changed."),
            view=None,
        )


# -- Past contests ----------------------------------------------------------


class PastContestSelect(discord.ui.Select):
    def __init__(self, sessions: list[ContestSession]):
        options = [
            discord.SelectOption(
                label=f"#{session.id} · {session.name}"[:100],
                value=str(session.id),
                emoji="🟢" if session.is_active else "🏁",
                description=(
                    f"{_local(session.started_at)} • {session.describe_duration()}"
                )[:100],
            )
            for session in sessions
        ]
        super().__init__(
            placeholder="Select a contest to view its leaderboard...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _deny_if_unauthorized(interaction):
            return

        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        session = await cog.get_contest(int(self.values[0]))
        if session is None:
            await interaction.followup.send("That contest no longer exists.", ephemeral=True)
            return

        standings, filter_stats, channels = snapshot_to_standings(session.snapshot)
        if not standings:
            # No frozen snapshot (older contest, or one superseded mid-run):
            # rebuild the standings straight from the stored events.
            standings, filter_stats, scan_stats = await cog.collect_standings(
                guild=interaction.guild,
                start=session.started_at,
                end=session.window_end,
                scan=False,
            )
            channels = scan_stats.channels_scanned

        embed = render_leaderboard_embed(
            standings=standings,
            filter_stats=filter_stats,
            channels_inspected=channels,
            channel_ids=await _leaderboard_channel_ids(interaction, session),
            period_label=describe_session_period(session),
            contest_name=session.name,
            is_final=not session.is_active,
        )
        await interaction.followup.send(
            content=(
                f"📜 Contest **#{session.id} · {session.name}** — "
                + ("still running" if session.is_active else f"ended {_local(session.window_end)}")
            ),
            embed=embed,
            view=RepublishContestView(session.id),
            ephemeral=True,
        )


class PastContestsView(discord.ui.View):
    def __init__(self, sessions: list[ContestSession]):
        super().__init__(timeout=300)
        self.add_item(PastContestSelect(sessions))


class RepublishContestView(discord.ui.View):
    """Lets a moderator re-post an archived leaderboard to the report channel."""

    def __init__(self, session_id: int):
        super().__init__(timeout=300)
        self.session_id = session_id

    @discord.ui.button(
        label="Publish to report channel", style=discord.ButtonStyle.primary, emoji="📤"
    )
    async def publish(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return

        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        session = await cog.get_contest(self.session_id)
        if session is None:
            await interaction.followup.send("That contest no longer exists.", ephemeral=True)
            return

        standings, filter_stats, channels = snapshot_to_standings(session.snapshot)
        if not standings:
            standings, filter_stats, scan_stats = await cog.collect_standings(
                guild=interaction.guild,
                start=session.started_at,
                end=session.window_end,
                scan=False,
            )
            channels = scan_stats.channels_scanned

        try:
            channel = await cog.resolve_report_channel(interaction.guild)
            embed = render_leaderboard_embed(
                standings=standings,
                filter_stats=filter_stats,
                channels_inspected=channels,
                channel_ids=await _leaderboard_channel_ids(interaction, session),
                period_label=describe_session_period(session),
                contest_name=session.name,
                requested_by=interaction.user,
                is_final=not session.is_active,
            )
            await channel.send(
                content=(
                    f"📜 **SafeRest Engagement Contest — {session.name}**\n"
                    f"Archived standings • Re-posted by {interaction.user.mention}"
                ),
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except (RuntimeError, discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(
                f"That leaderboard could not be published: {exc}", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Archived leaderboard for **{session.name}** posted in {channel.mention}.",
            ephemeral=True,
        )


# -- Custom range report ----------------------------------------------------


class CustomContestDateModal(discord.ui.Modal, title="Custom Contest Date Range"):
    start_date = discord.ui.TextInput(
        label=f"Start date/time ({REPORT_TIMEZONE})",
        placeholder="YYYY-MM-DD or YYYY-MM-DD HH:MM",
        min_length=10,
        max_length=16,
    )
    end_date = discord.ui.TextInput(
        label=f"End date/time ({REPORT_TIMEZONE})",
        placeholder="YYYY-MM-DD or YYYY-MM-DD HH:MM (blank for now)",
        min_length=0,
        max_length=16,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _deny_if_unauthorized(interaction):
            return
        try:
            start, end = _parse_custom_contest_range(
                self.start_date.value, self.end_date.value
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        period_label = (
            f"Custom: {self.start_date.value.strip()} to "
            f"{self.end_date.value.strip() or 'Now'} ({REPORT_TIMEZONE})"
        )
        await _run_contest_publish(
            interaction, start=start, end=end, period_label=period_label
        )


class EngagementContestRangeSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Select an ad-hoc reporting period...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Starting Today",
                    value="today",
                    emoji="📅",
                    description=f"Since 12:00 AM today ({REPORT_TIMEZONE})",
                ),
                discord.SelectOption(
                    label="Past 24 Hours",
                    value="24h",
                    emoji="⏱️",
                    description="Last 24 hours from right now",
                ),
                discord.SelectOption(
                    label="Past 7 Days", value="7d", emoji="🗓️", description="Last 7 days"
                ),
                discord.SelectOption(
                    label="Past 14 Days", value="14d", emoji="📆", description="Last 14 days"
                ),
                discord.SelectOption(
                    label="Past 30 Days", value="30d", emoji="📊", description="Last 30 days"
                ),
                discord.SelectOption(
                    label="Custom Date Range",
                    value="custom",
                    emoji="⚙️",
                    description="Specify custom start and end dates",
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _deny_if_unauthorized(interaction):
            return

        selection = self.values[0]
        if selection == "custom":
            await interaction.response.send_modal(CustomContestDateModal())
            return

        now_utc = datetime.now(timezone.utc)
        if selection == "today":
            start, end, label = get_today_range()
        elif selection == "24h":
            start, end, label = now_utc - timedelta(hours=24), now_utc, "Past 24 Hours"
        elif selection == "7d":
            start, end, label = now_utc - timedelta(days=7), now_utc, "Past 7 Days"
        elif selection == "14d":
            start, end, label = now_utc - timedelta(days=14), now_utc, "Past 14 Days"
        elif selection == "30d":
            start, end, label = now_utc - timedelta(days=30), now_utc, "Past 30 Days"
        else:
            await interaction.response.send_message("Invalid option.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await _run_contest_publish(interaction, start=start, end=end, period_label=label)


class EngagementContestRangeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.add_item(EngagementContestRangeSelect())


# -- Settings ---------------------------------------------------------------


def build_contest_settings_embed(
    settings: ContestSettings, notice: str | None = None
) -> discord.Embed:
    lines = []
    if notice:
        lines += [f"✅ {notice}", embed_builder.SEPARATOR]
    counted = (
        _channel_mentions(settings.channel_ids)
        if settings.channel_ids
        else "*none — nothing is scored until you pick at least one*"
    )
    leaderboard = (
        f"<#{settings.report_channel_id}>" if settings.report_channel_id else "*not set*"
    )
    lines += [
        embed_builder.item(f"**Counted channels ({len(settings.channel_ids)}):** {counted}")
        + "\n*Messages, TikTok posts and reactions here (and in their threads) earn "
        f"points. Up to {MAX_CONTEST_CHANNELS} channels.*",
        embed_builder.item(f"**Leaderboard channel:** {leaderboard}")
        + "\n*Where the Top 15 Leaderboard and final results are published.*",
        embed_builder.SEPARATOR,
        "Changes take effect immediately, including for a contest that is running. "
        "Empty the first menu to pause counting.",
    ]
    return embed_builder.base_embed(
        title="⚙️  Engagement Contest settings",
        description="\n".join(lines),
        color=embed_builder.COLOR_ACCENT,
    )


async def _render_contest_settings(
    interaction: discord.Interaction, notice: str | None = None
) -> None:
    settings = await _get_cog(interaction).settings(interaction.guild.id)
    await replace_message(
        interaction,
        build_contest_settings_embed(settings, notice),
        ContestSettingsView(settings),
    )


class ContestChannelsSelect(discord.ui.ChannelSelect):
    def __init__(self, settings: ContestSettings):
        super().__init__(
            placeholder="Counted channels — pick every channel that should score",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=0,
            max_values=MAX_CONTEST_CHANNELS,
            default_values=[
                discord.Object(cid)
                for cid in sorted(settings.channel_ids)[:MAX_CONTEST_CHANNELS]
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        chosen = [channel.id for channel in self.values]
        await _get_cog(interaction).set_channels(interaction.guild.id, chosen)
        await _render_contest_settings(
            interaction,
            f"Now counting **{len(chosen)}** channel(s)."
            if chosen
            else "Counted channels cleared — nothing is scored until you pick some.",
        )


class ContestReportChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, settings: ContestSettings):
        super().__init__(
            placeholder="Leaderboard channel — not set",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=0,
            max_values=1,
            default_values=(
                [discord.Object(settings.report_channel_id)]
                if settings.report_channel_id
                else []
            ),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        chosen = self.values[0].id if self.values else None
        await _get_cog(interaction).set_report_channel(interaction.guild.id, chosen)
        await _render_contest_settings(
            interaction,
            f"Leaderboards will be published in <#{chosen}>."
            if chosen
            else "Leaderboard channel cleared.",
        )


class ContestSettingsView(discord.ui.View):
    def __init__(self, settings: ContestSettings):
        super().__init__(timeout=600)
        self.add_item(ContestChannelsSelect(settings))
        self.add_item(ContestReportChannelSelect(settings))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await _deny_if_unauthorized(interaction)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        settings = await _get_cog(interaction).settings(interaction.guild.id)
        await replace_message(
            interaction,
            embed_builder.success_embed(
                "Contest settings saved",
                f"Counting **{len(settings.channel_ids)}** channel(s). "
                "Press **🔄 Refresh** on the contest panel to see them.",
            ),
            None,
        )
        self.stop()


# -- The hub ----------------------------------------------------------------


class EngagementContestHubView(discord.ui.View):
    """Start, publish, end, review and reset — the whole contest in one panel."""

    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(
        label="Start New Contest", style=discord.ButtonStyle.success, emoji="🚀", row=0
    )
    async def start_contest(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return
        cog = _get_cog(interaction)
        if cog is not None and interaction.guild is not None:
            settings = await cog.settings(interaction.guild.id)
            if not settings.channel_ids:
                await interaction.response.send_message(
                    embed=embed_builder.warning_embed(
                        "Choose the contest channels first",
                        "No channels are counted yet. Press **⚙️ Settings** and pick "
                        "the channels whose messages and reactions should score.",
                    ),
                    ephemeral=True,
                )
                return
        await interaction.response.send_modal(StartContestModal())

    @discord.ui.button(
        label="Publish Leaderboard", style=discord.ButtonStyle.primary, emoji="🏆", row=0
    )
    async def publish_leaderboard(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return

        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready.", ephemeral=True
            )
            return

        session = await cog.get_active_contest(interaction.guild.id)
        if session is None:
            await interaction.response.send_message(
                embed=embed_builder.warning_embed(
                    "No contest is running",
                    "Press **Start New Contest** first, or use **Ad-hoc Report** "
                    "to score an arbitrary date range.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await _run_contest_publish(
            interaction,
            start=session.started_at,
            end=datetime.now(timezone.utc),
            period_label=describe_session_period(session),
            session=session,
        )

    @discord.ui.button(
        label="End Contest", style=discord.ButtonStyle.danger, emoji="🛑", row=0
    )
    async def end_contest(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return

        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready.", ephemeral=True
            )
            return

        session = await cog.get_active_contest(interaction.guild.id)
        if session is None:
            await interaction.response.send_message(
                embed=embed_builder.warning_embed(
                    "No contest is running", "There is nothing to end right now."
                ),
                ephemeral=True,
            )
            return

        confirm = ConfirmActionView(
            confirm_label="End it and publish final results",
            style=discord.ButtonStyle.danger,
            parent=interaction,
        )
        await interaction.response.send_message(
            embed=embed_builder.warning_embed(
                f"End {session.name}?",
                (
                    "The final standings will be frozen into the archive and "
                    "published as **FINAL RESULTS**. Counting stops immediately.\n\n"
                    f"{embed_builder.item(f'Running since **{_local(session.started_at)}**')}\n"
                    f"{embed_builder.item(f'Duration: **{session.describe_duration()}**')}\n"
                    f"{embed_builder.item('You can still view it later under **Past Contests**.')}"
                ),
            ),
            view=confirm,
            ephemeral=True,
        )
        await confirm.wait()
        if not confirm.confirmed:
            return

        await interaction.edit_original_response(
            embed=embed_builder.info_embed(
                "Ending the contest…",
                "Scanning the contest channels and freezing the final standings.",
            ),
            view=None,
        )

        try:
            ended, standings = await cog.end_contest(
                guild=interaction.guild, session=session, ended_by=interaction.user
            )
            channel = await cog.resolve_report_channel(interaction.guild)
            snapshot_standings, filter_stats, channels = snapshot_to_standings(
                ended.snapshot
            )
            embed = render_leaderboard_embed(
                standings=snapshot_standings or standings,
                filter_stats=filter_stats,
                channels_inspected=channels,
                channel_ids=await _leaderboard_channel_ids(interaction, ended),
                period_label=describe_session_period(ended),
                contest_name=ended.name,
                requested_by=interaction.user,
                is_final=True,
            )
            await channel.send(
                content=(
                    f"🏁 **SafeRest Engagement Contest — FINAL RESULTS**\n"
                    f"**{ended.name}** • Closed by {interaction.user.mention}"
                ),
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except (RuntimeError, discord.Forbidden, discord.HTTPException) as exc:
            logger.exception("Could not close the engagement contest cleanly")
            await interaction.followup.send(
                f"The contest was closed, but the final results could not be "
                f"published: {exc}",
                ephemeral=True,
            )
            return

        winner = (
            f"🥇 Winner: **{standings[0].display_name}** "
            f"({standings[0].total_points:,} pts)"
            if standings
            else "No qualifying participants"
        )
        await interaction.followup.send(
            embed=embed_builder.success_embed(
                f"{ended.name} is closed",
                (
                    f"{embed_builder.item(winner)}\n"
                    f"{embed_builder.item(f'Final standings published in {channel.mention}')}\n"
                    f"{embed_builder.item(f'Ran for **{ended.describe_duration()}**')}\n"
                    f"{embed_builder.item('Archived under **Past Contests**.')}"
                ),
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Past Contests", style=discord.ButtonStyle.secondary, emoji="📜", row=1
    )
    async def past_contests(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return

        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        sessions = await cog.list_contests(interaction.guild.id)
        if not sessions:
            await interaction.followup.send(
                embed=embed_builder.info_embed(
                    "No contests yet",
                    "Once you start and end a contest it is archived here with its "
                    "final leaderboard.",
                ),
                ephemeral=True,
            )
            return

        lines = [
            embed_builder.item(
                f"**#{session.id} · {session.name}** — "
                + (
                    "🟢 running"
                    if session.is_active
                    else f"🏁 ended {_local(session.window_end)}"
                )
                + f" • {session.describe_duration()}"
            )
            for session in sessions
        ]
        await interaction.followup.send(
            embed=embed_builder.base_embed(
                title="📜  Past Contests",
                description=(
                    f"{embed_builder.SEPARATOR}\n"
                    f"{len(sessions)} contest(s) on record. Pick one below to see "
                    "its leaderboard.\n"
                    f"{embed_builder.SEPARATOR}\n\n" + "\n".join(lines)
                ),
                color=embed_builder.COLOR_ACCENT,
            ),
            view=PastContestsView(sessions),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ad-hoc Report", style=discord.ButtonStyle.secondary, emoji="⚙️", row=1
    )
    async def adhoc_report(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return
        await interaction.response.send_message(
            embed=embed_builder.base_embed(
                title="⚙️  Ad-hoc Contest Report",
                description=(
                    "Score an arbitrary date range without touching the running "
                    "contest. Useful for previews and spot checks.\n\n"
                    + SCORING_RULES
                ),
                color=embed_builder.COLOR_ACCENT,
            ),
            view=EngagementContestRangeView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Reset All Counts", style=discord.ButtonStyle.danger, emoji="♻️", row=1
    )
    async def reset_counts(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return

        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready.", ephemeral=True
            )
            return

        confirm = ConfirmActionView(
            confirm_label="Wipe everything",
            style=discord.ButtonStyle.danger,
            parent=interaction,
        )
        await interaction.response.send_message(
            embed=embed_builder.error_embed(
                "Reset every recorded count?",
                (
                    "This permanently deletes **all** contest events **and every "
                    "archived past contest** for this server. It cannot be undone.\n\n"
                    f"{embed_builder.item('Use this to start the contest system from scratch.')}\n"
                    f"{embed_builder.item('To simply begin a new round instead, press **Start New Contest** — old contests stay archived.')}"
                ),
            ),
            view=confirm,
            ephemeral=True,
        )
        await confirm.wait()
        if not confirm.confirmed:
            return

        events, sessions = await cog.reset_contest_data(interaction.guild)
        logger.warning(
            "Contest data wiped for guild %s by %s",
            interaction.guild.id,
            interaction.user,
        )
        await interaction.edit_original_response(
            embed=embed_builder.success_embed(
                "All contest data cleared",
                (
                    f"{embed_builder.item(f'Removed **{events:,}** recorded event(s)')}\n"
                    f"{embed_builder.item(f'Removed **{sessions:,}** archived contest(s)')}\n"
                    f"{embed_builder.item('Press **Start New Contest** to begin counting again.')}"
                ),
            ),
            view=None,
        )

    @discord.ui.button(
        label="Settings", style=discord.ButtonStyle.primary, emoji="⚙️", row=2
    )
    async def settings(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return
        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            await interaction.response.send_message(
                "The engagement contest module is not ready.", ephemeral=True
            )
            return
        settings = await cog.settings(interaction.guild.id)
        await interaction.response.send_message(
            embed=build_contest_settings_embed(settings),
            view=ContestSettingsView(settings),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=2
    )
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await _deny_if_unauthorized(interaction):
            return
        await interaction.response.edit_message(
            embed=await build_contest_hub_embed(interaction),
            view=EngagementContestHubView(),
        )
