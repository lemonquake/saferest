"""Moderator interactions for selecting and generating engagement reports."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from config import REPORT_TIMEZONE

logger = logging.getLogger("saferest_bot.engagement_ui")


def _report_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(REPORT_TIMEZONE)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown REPORT_TIMEZONE %r; falling back to UTC", REPORT_TIMEZONE)
        return ZoneInfo("UTC")


def _can_manage(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


async def _publish_from_interaction(
    interaction: discord.Interaction,
    *,
    start: datetime,
    end: datetime,
    period_label: str,
) -> None:
    if interaction.guild is None:
        await interaction.followup.send(
            "Engagement reports can only be generated inside a server.", ephemeral=True
        )
        return
    cog = interaction.client.get_cog("EngagementCog")
    if cog is None:
        await interaction.followup.send(
            "The engagement tracker is not available. Check the bot logs.", ephemeral=True
        )
        return

    try:
        channel, member_count, ranking_limit, sync_stats = await cog.publish_report(
            guild=interaction.guild,
            start=start,
            end=end,
            period_label=period_label,
            requested_by=interaction.user,
        )
    except (RuntimeError, discord.Forbidden, discord.HTTPException) as exc:
        logger.exception("Could not publish engagement report")
        await interaction.followup.send(
            f"The report could not be published: {exc}", ephemeral=True
        )
        return
    except Exception:
        logger.exception("Unexpected engagement report failure")
        await interaction.followup.send(
            "The history scan failed unexpectedly. Check the bot console for the error.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"Report published in {channel.mention}. Evaluated {member_count:,} members "
        f"with a Top {ranking_limit:,} ranking cap after inspecting "
        f"{sync_stats.messages_scanned:,} historical messages.",
        ephemeral=True,
    )


def _parse_custom_range(start_text: str, end_text: str) -> tuple[datetime, datetime]:
    tz = _report_timezone()

    def parse(value: str, *, is_end: bool) -> datetime:
        clean_value = value.strip()
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


class CustomReportDateModal(discord.ui.Modal, title="Custom Engagement Report"):
    start_date = discord.ui.TextInput(
        label=f"Start date/time ({REPORT_TIMEZONE})",
        placeholder="YYYY-MM-DD or YYYY-MM-DD HH:MM",
        min_length=10,
        max_length=16,
    )
    end_date = discord.ui.TextInput(
        label=f"End date/time ({REPORT_TIMEZONE})",
        placeholder="YYYY-MM-DD or YYYY-MM-DD HH:MM",
        min_length=10,
        max_length=16,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message(
                "You need the Manage Server permission to generate reports.",
                ephemeral=True,
            )
            return
        try:
            start, end = _parse_custom_range(self.start_date.value, self.end_date.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await _publish_from_interaction(
            interaction,
            start=start,
            end=end,
            period_label=(
                f"Custom: {self.start_date.value.strip()} to {self.end_date.value.strip()} "
                f"({REPORT_TIMEZONE})"
            ),
        )


class EngagementRangeSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose a report period...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Past 1 day", value="day", emoji="📅", description="Last 24 hours"
                ),
                discord.SelectOption(
                    label="Past 7 days", value="week", emoji="🗓️", description="Last 7 days"
                ),
                discord.SelectOption(
                    label="Past 14 days", value="fortnight", emoji="📆", description="Last 14 days"
                ),
                discord.SelectOption(
                    label="Past 1 month", value="month", emoji="📊", description="Last 30 days"
                ),
                discord.SelectOption(
                    label="Custom date range", value="custom", emoji="⚙️", description="Choose exact dates"
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message(
                "You need the Manage Server permission to generate reports.",
                ephemeral=True,
            )
            return
        selection = self.values[0]
        if selection == "custom":
            await interaction.response.send_modal(CustomReportDateModal())
            return

        days, label = {
            "day": (1, "Past 1 day"),
            "week": (7, "Past 7 days"),
            "fortnight": (14, "Past 14 days"),
            "month": (30, "Past 1 month (30 days)"),
        }[selection]
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _publish_from_interaction(
            interaction, start=start, end=end, period_label=label
        )


class EngagementRangeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(EngagementRangeSelect())
