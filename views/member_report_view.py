"""
Moderator interface for SafeRest community growth reports.
Created by Aljay Leodones for SafeRest.

Flow:  choose how many days  ->  preview it, or deploy a live report that
       refreshes itself in a public channel.
"""

from __future__ import annotations

import logging

import discord

from cogs.memberreport import MAX_CUSTOM_DAYS
from config import REPORT_REFRESH_HOURS
from utils import embed_builder
from views.common import missing_cog, require_manage

logger = logging.getLogger("saferest_bot.report_ui")

TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]

# Ready-made windows offered in the day picker.
WINDOW_CHOICES: list[tuple[str, int, str]] = [
    ("Today · vs yesterday", 1, "📅"),
    ("Last 7 days · vs a week ago", 7, "🗓️"),
    ("Last 14 days · vs two weeks ago", 14, "📆"),
    ("Last 30 days · vs a month ago", 30, "📊"),
    ("Last 90 days · vs a quarter ago", 90, "📈"),
]


def _report_cog(interaction: discord.Interaction):
    return interaction.client.get_cog("MemberReportCog")


def _window_label(days: int) -> str:
    if days == 1:
        return "Daily · today vs yesterday"
    if days == 7:
        return "Weekly · last 7 days"
    if days == 30:
        return "Monthly · last 30 days"
    return f"Custom · last {days} days"


def _timeframe_key(days: int) -> str:
    return {1: "daily", 7: "weekly", 30: "monthly"}.get(days, f"custom{days}")


# ── Custom day count ───────────────────────────────────────────────────────
class CustomDaysModal(discord.ui.Modal, title="Custom Report Window"):
    def __init__(self, mode: str):
        super().__init__(timeout=600)
        self.mode = mode
        self.days_input = discord.ui.TextInput(
            label="How many days should the report cover?",
            placeholder=f"A number between 1 and {MAX_CUSTOM_DAYS} (example: 21)",
            max_length=3,
            required=True,
        )
        self.add_item(self.days_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        raw = self.days_input.value.strip()
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_CUSTOM_DAYS:
            await interaction.response.send_message(
                embed=embed_builder.warning_embed(
                    "That day count is not valid",
                    f"Enter a whole number between 1 and {MAX_CUSTOM_DAYS}.",
                ),
                ephemeral=True,
            )
            return
        await _continue_with_days(interaction, days=int(raw), mode=self.mode)


# ── Day picker ─────────────────────────────────────────────────────────────
class ReportWindowSelect(discord.ui.Select):
    def __init__(self, mode: str):
        self.mode = mode
        options = [
            discord.SelectOption(
                label=label,
                value=str(days),
                emoji=emoji,
                description=f"Compares the last {days} day(s) against the same window before it",
            )
            for label, days, emoji in WINDOW_CHOICES
        ]
        options.append(
            discord.SelectOption(
                label="Custom number of days...",
                value="custom",
                emoji="⚙️",
                description=f"Pick any window from 1 to {MAX_CUSTOM_DAYS} days",
            )
        )
        super().__init__(
            placeholder="Select which days the report should cover...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        if self.values[0] == "custom":
            await interaction.response.send_modal(CustomDaysModal(self.mode))
            return
        await _continue_with_days(interaction, days=int(self.values[0]), mode=self.mode)


class ReportWindowView(discord.ui.View):
    """`mode` is either `preview` (ephemeral) or `deploy` (public + live)."""

    def __init__(self, mode: str):
        super().__init__(timeout=600)
        self.add_item(ReportWindowSelect(mode))


async def _continue_with_days(
    interaction: discord.Interaction, *, days: int, mode: str
) -> None:
    cog = _report_cog(interaction)
    if cog is None:
        await missing_cog(interaction, "Community reporting")
        return

    if mode == "preview":
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.reconcile_joins_from_members(interaction.guild)
        embed = await cog.compose_report(
            interaction.guild, days=days, label=_window_label(days)
        )
        await interaction.followup.send(
            content="Preview only — nothing was posted publicly.",
            embed=embed,
            ephemeral=True,
        )
        return

    channels_hint = (
        f"The report is posted once and then refreshes itself every "
        f"**{REPORT_REFRESH_HOURS} hours** — no reposting needed."
    )
    await interaction.response.edit_message(
        embed=embed_builder.base_embed(
            title="Where should this live report live?",
            description=(
                f"{embed_builder.item(f'Window: **{_window_label(days)}**')}\n"
                f"{embed_builder.item(channels_hint)}\n"
                f"{embed_builder.SEPARATOR}\n"
                "Choose the channel below."
            ),
        ),
        view=ReportChannelView(days=days),
    )


# ── Channel choice for a deployed report ───────────────────────────────────
class ReportChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, days: int):
        super().__init__(
            placeholder="Choose the channel for the live report...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
        )
        self.days = days

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        cog = _report_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Community reporting")
            return
        channel = interaction.guild.get_channel(self.values[0].id)
        if channel is None:
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "Channel unavailable", "That channel could not be opened."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=embed_builder.info_embed(
                "Building the report...",
                "Collecting growth baselines, join history and engagement.",
            ),
            view=None,
        )
        try:
            message = await cog.deploy_report(
                guild=interaction.guild,
                channel=channel,
                timeframe=_timeframe_key(self.days),
                days=self.days,
                label=_window_label(self.days),
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                embed=embed_builder.error_embed(
                    "Missing permissions",
                    "The bot needs **Send Messages** and **Embed Links** in "
                    f"{channel.mention}.",
                ),
                view=None,
            )
            return
        except discord.HTTPException as exc:
            logger.exception("Could not deploy the community report")
            await interaction.edit_original_response(
                embed=embed_builder.error_embed("Deployment failed", str(exc)),
                view=None,
            )
            return

        await interaction.edit_original_response(
            embed=embed_builder.success_embed(
                "Live report deployed",
                "\n".join(
                    [
                        embed_builder.item(f"Channel: {channel.mention}"),
                        embed_builder.item(f"Window: **{_window_label(self.days)}**"),
                        embed_builder.item(
                            f"Auto-refresh: every **{REPORT_REFRESH_HOURS} hours** "
                            "and on every bot start"
                        ),
                        embed_builder.item(f"[Jump to the report]({message.jump_url})"),
                    ]
                ),
            ),
            view=None,
        )


class ReportChannelView(discord.ui.View):
    def __init__(self, days: int):
        super().__init__(timeout=600)
        self.add_item(ReportChannelSelect(days))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=embed_builder.info_embed("Cancelled", "No report was deployed."),
            view=None,
        )
        self.stop()


# ── Managing deployed reports ──────────────────────────────────────────────
class ReportRemoveSelect(discord.ui.Select):
    def __init__(self, reports: list[dict]):
        super().__init__(
            placeholder="Select a live report to stop refreshing...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=report["label"] or report["timeframe"],
                    value=report["timeframe"],
                    emoji="🗑️",
                    description=f"Last updated: {report['updated_at']} UTC",
                )
                for report in reports[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        cog = _report_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Community reporting")
            return
        removed = await cog.remove_report(interaction.guild.id, self.values[0])
        embed = (
            embed_builder.success_embed(
                "Live report stopped",
                "The posted message stays in the channel, but it will no longer "
                "update itself.",
            )
            if removed
            else embed_builder.warning_embed(
                "Nothing to remove", "That report was already stopped."
            )
        )
        await interaction.response.edit_message(embed=embed, view=None)


class ReportManageView(discord.ui.View):
    def __init__(self, reports: list[dict]):
        super().__init__(timeout=600)
        self.add_item(ReportRemoveSelect(reports))


# ── Persistent reporting hub ───────────────────────────────────────────────
async def build_report_hub_embed(interaction: discord.Interaction) -> discord.Embed:
    cog = _report_cog(interaction)
    if cog is None:
        return embed_builder.error_embed(
            "Community reporting unavailable", "The reporting module did not load."
        )
    reports = await cog.list_reports(interaction.guild.id)
    if reports:
        deployed = "\n".join(
            embed_builder.item(
                f"**{report['label'] or report['timeframe']}** in <#{report['channel_id']}> "
                f"· updated {report['updated_at']} UTC"
            )
            for report in reports
        )
    else:
        deployed = embed_builder.item("No live reports deployed yet.")

    how_lines = [
        embed_builder.item("**Preview** — see the numbers privately, nothing is posted."),
        embed_builder.item(
            "**Deploy** — post one public report that refreshes itself every "
            f"{REPORT_REFRESH_HOURS} hours."
        ),
        embed_builder.item("**Refresh now** — update every live report immediately."),
    ]
    return embed_builder.base_embed(
        title="📊  Community Reports",
        description="\n".join(
            [
                "Track how the community is growing: member deltas against a stored "
                "baseline, joins per day, monthly trends and the most active members.",
                embed_builder.SEPARATOR,
                embed_builder.section("Live reports"),
                deployed,
                "",
                embed_builder.section("How to use it"),
                "\n".join(how_lines),
            ]
        ),
    )


class MemberReportHubView(discord.ui.View):
    """Persistent reporting hub (static custom_ids, survives restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Preview a report",
        style=discord.ButtonStyle.primary,
        custom_id="saferest_report:preview",
        emoji="👀",
        row=0,
    )
    async def preview(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        await interaction.response.send_message(
            embed=embed_builder.base_embed(
                title="Preview a community report",
                description=(
                    "Select which days to cover. The preview is private — nothing is "
                    "posted to the server."
                ),
            ),
            view=ReportWindowView("preview"),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Deploy live report",
        style=discord.ButtonStyle.success,
        custom_id="saferest_report:deploy",
        emoji="📌",
        row=0,
    )
    async def deploy(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        await interaction.response.send_message(
            embed=embed_builder.base_embed(
                title="Deploy a live community report",
                description=(
                    "Select which days the report should cover. It is posted once and "
                    f"then updates itself every {REPORT_REFRESH_HOURS} hours."
                ),
            ),
            view=ReportWindowView("deploy"),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Refresh now",
        style=discord.ButtonStyle.secondary,
        custom_id="saferest_report:refresh",
        emoji="🔄",
        row=1,
    )
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        cog = _report_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Community reporting")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.record_snapshot(interaction.guild)
        await cog.reconcile_joins_from_members(interaction.guild)
        refreshed = await cog.refresh_all_reports(interaction.guild)
        await interaction.followup.send(
            embed=embed_builder.success_embed(
                "Reports refreshed",
                embed_builder.item(f"Updated **{refreshed}** live report(s).")
                + "\n"
                + embed_builder.item("Today's growth baseline was recorded."),
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Manage live reports",
        style=discord.ButtonStyle.secondary,
        custom_id="saferest_report:manage",
        emoji="🗂️",
        row=1,
    )
    async def manage(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await require_manage(interaction):
            return
        cog = _report_cog(interaction)
        if cog is None:
            await missing_cog(interaction, "Community reporting")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reports = await cog.list_reports(interaction.guild.id)
        if not reports:
            await interaction.followup.send(
                embed=embed_builder.info_embed(
                    "No live reports yet",
                    "Use **Deploy live report** to publish one.",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=await build_report_hub_embed(interaction),
            view=ReportManageView(reports),
            ephemeral=True,
        )
