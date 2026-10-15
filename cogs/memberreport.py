"""
Community growth reporting: daily snapshots, join tallies and live reports.
Created by Aljay Leodones for SafeRest.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging

import discord
from discord.ext import commands, tasks

from config import (
    BRAND_NAME,
    COMMUNITY_DATABASE_PATH,
    REPORT_REFRESH_HOURS,
)
from core.database import get_database
from utils import embed_builder

logger = logging.getLogger("saferest_bot.memberreport")

# Selectable reporting windows. `custom` is filled in from the day picker.
TIMEFRAMES: dict[str, tuple[int, str]] = {
    "daily": (1, "Daily · vs yesterday"),
    "weekly": (7, "Weekly · vs 7 days ago"),
    "monthly": (30, "Monthly · vs 30 days ago"),
}
MAX_CUSTOM_DAYS = 365


@dataclass(slots=True)
class GuildStats:
    total: int
    humans: int
    bots: int
    online: int
    admins: int


@dataclass(slots=True)
class ActiveMember:
    display_name: str
    points: int


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


class MemberReportCog(commands.Cog, name="MemberReportCog"):
    """Records growth baselines and publishes self-refreshing public reports."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_database(COMMUNITY_DATABASE_PATH)

    async def cog_load(self) -> None:
        await self.db.initialize()
        self.daily_snapshot_job.start()
        self.refresh_reports_job.start()
        logger.info(
            "Community reporting ready (live reports refresh every %sh)",
            REPORT_REFRESH_HOURS,
        )

    async def cog_unload(self) -> None:
        self.daily_snapshot_job.cancel()
        self.refresh_reports_job.cancel()

    # -- listeners ---------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self.record_join(member.guild.id, datetime.now(timezone.utc).date())

    # -- snapshots ---------------------------------------------
    def collect_stats(self, guild: discord.Guild) -> GuildStats:
        humans = sum(1 for member in guild.members if not member.bot)
        bots = sum(1 for member in guild.members if member.bot)
        online = sum(
            1
            for member in guild.members
            if not member.bot and member.status is not discord.Status.offline
        )
        admins = sum(
            1
            for member in guild.members
            if not member.bot and member.guild_permissions.administrator
        )
        total = guild.member_count or (humans + bots)
        return GuildStats(total=total, humans=humans, bots=bots, online=online, admins=admins)

    @property
    def presence_available(self) -> bool:
        return bool(self.bot.intents.presences)

    async def record_snapshot(self, guild: discord.Guild) -> GuildStats:
        """Stores (or refreshes) today's UTC baseline row for this server."""
        stats = self.collect_stats(guild)
        await self.db.execute(
            """
            INSERT INTO member_snapshots
                (guild_id, snapshot_date, total, humans, bots, online, admins)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, snapshot_date) DO UPDATE SET
                total = excluded.total,
                humans = excluded.humans,
                bots = excluded.bots,
                online = MAX(member_snapshots.online, excluded.online),
                admins = excluded.admins
            """,
            (
                guild.id,
                _utc_today().isoformat(),
                stats.total,
                stats.humans,
                stats.bots,
                stats.online,
                stats.admins,
            ),
        )
        return stats

    async def get_baseline(self, guild_id: int, days: int) -> GuildStats | None:
        """Newest snapshot on or before `days` ago, used for growth deltas."""
        target = (_utc_today() - timedelta(days=days)).isoformat()
        row = await self.db.fetchone(
            """
            SELECT total, humans, bots, online, admins
            FROM member_snapshots
            WHERE guild_id = ? AND snapshot_date <= ?
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            (guild_id, target),
        )
        if row is None:
            return None
        return GuildStats(
            total=row["total"],
            humans=row["humans"],
            bots=row["bots"],
            online=row["online"],
            admins=row["admins"],
        )

    # -- join tallies ------------------------------------------
    async def record_join(self, guild_id: int, day: date) -> None:
        await self.db.execute(
            """
            INSERT INTO member_joins (guild_id, period, period_date, joins)
            VALUES (?, 'day', ?, 1)
            ON CONFLICT(guild_id, period, period_date)
            DO UPDATE SET joins = joins + 1
            """,
            (guild_id, day.isoformat()),
        )
        await self.db.execute(
            """
            INSERT INTO member_joins (guild_id, period, period_date, joins)
            VALUES (?, 'week', ?, 1)
            ON CONFLICT(guild_id, period, period_date)
            DO UPDATE SET joins = joins + 1
            """,
            (guild_id, _week_start(day).isoformat()),
        )

    async def reconcile_joins_from_members(self, guild: discord.Guild) -> int:
        """
        Rebuilds join tallies from `member.joined_at`.

        Live join events are missed while the bot is offline, so the stored
        tally is merged with the recomputed one using MAX() on every startup.
        """
        daily: dict[str, int] = defaultdict(int)
        weekly: dict[str, int] = defaultdict(int)
        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue
            joined = member.joined_at.astimezone(timezone.utc).date()
            daily[joined.isoformat()] += 1
            weekly[_week_start(joined).isoformat()] += 1

        await self.db.executemany(
            """
            INSERT INTO member_joins (guild_id, period, period_date, joins)
            VALUES (?, 'day', ?, ?)
            ON CONFLICT(guild_id, period, period_date)
            DO UPDATE SET joins = MAX(joins, excluded.joins)
            """,
            [(guild.id, day, count) for day, count in daily.items()],
        )
        await self.db.executemany(
            """
            INSERT INTO member_joins (guild_id, period, period_date, joins)
            VALUES (?, 'week', ?, ?)
            ON CONFLICT(guild_id, period, period_date)
            DO UPDATE SET joins = MAX(joins, excluded.joins)
            """,
            [(guild.id, week, count) for week, count in weekly.items()],
        )
        return len(daily)

    async def get_daily_joins(self, guild_id: int, days: int) -> list[tuple[date, int]]:
        """Join counts for the last `days` calendar days (oldest first)."""
        start = (_utc_today() - timedelta(days=days - 1)).isoformat()
        rows = await self.db.fetchall(
            """
            SELECT period_date, joins FROM member_joins
            WHERE guild_id = ? AND period = 'day' AND period_date >= ?
            """,
            (guild_id, start),
        )
        stored = {row["period_date"]: row["joins"] for row in rows}
        today = _utc_today()
        return [
            (
                today - timedelta(days=offset),
                stored.get((today - timedelta(days=offset)).isoformat(), 0),
            )
            for offset in reversed(range(days))
        ]

    async def get_joins_in_window(self, guild_id: int, days: int) -> int:
        start = (_utc_today() - timedelta(days=days - 1)).isoformat()
        row = await self.db.fetchone(
            """
            SELECT COALESCE(SUM(joins), 0) AS total FROM member_joins
            WHERE guild_id = ? AND period = 'day' AND period_date >= ?
            """,
            (guild_id, start),
        )
        return row["total"] if row else 0

    async def get_monthly_joins(self, guild_id: int, months: int) -> list[tuple[str, int]]:
        rows = await self.db.fetchall(
            """
            SELECT substr(period_date, 1, 7) AS month, SUM(joins) AS joins
            FROM member_joins
            WHERE guild_id = ? AND period = 'day'
            GROUP BY month
            ORDER BY month DESC
            LIMIT ?
            """,
            (guild_id, months),
        )
        return [(row["month"], row["joins"]) for row in reversed(rows)]

    async def get_prev_month_to_date(self, guild_id: int) -> int:
        """Joins during the same slice of last month, for a fair comparison."""
        today = _utc_today()
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        first_last_month = last_month_end.replace(day=1)
        day_cap = min(today.day, last_month_end.day)
        cutoff = first_last_month.replace(day=day_cap)
        row = await self.db.fetchone(
            """
            SELECT COALESCE(SUM(joins), 0) AS total FROM member_joins
            WHERE guild_id = ? AND period = 'day'
              AND period_date >= ? AND period_date <= ?
            """,
            (guild_id, first_last_month.isoformat(), cutoff.isoformat()),
        )
        return row["total"] if row else 0

    # -- engagement bridge -------------------------------------
    async def top_active_members(
        self, guild: discord.Guild, days: int, limit: int = 5
    ) -> list[ActiveMember]:
        """Top contributors for the window, sourced from the engagement ledger."""
        cog = self.bot.get_cog("EngagementCog")
        if cog is None:
            return []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        try:
            rows = await cog.store.aggregate(guild.id, start, end)
        except RuntimeError:
            return []

        ranked = sorted(rows, key=lambda row: row.total, reverse=True)
        active: list[ActiveMember] = []
        for row in ranked:
            if row.total <= 0:
                break
            member = guild.get_member(row.user_id)
            name = member.display_name if member else row.display_name
            active.append(ActiveMember(display_name=name, points=row.total))
            if len(active) >= limit:
                break
        return active

    # -- report rendering --------------------------------------
    def build_report_embed(
        self,
        *,
        guild: discord.Guild,
        label: str,
        days: int,
        stats: GuildStats,
        baseline: GuildStats | None,
        daily_joins: list[tuple[date, int]],
        window_joins: int,
        monthly_joins: list[tuple[str, int]],
        prev_month_to_date: int,
        top_active: list[ActiveMember],
    ) -> discord.Embed:
        embed = embed_builder.base_embed(
            title=f"{BRAND_NAME} — Community Report",
            description=(
                f"**{label}**\n"
                f"A live snapshot of how the **{guild.name}** community is growing.\n"
                f"{embed_builder.SEPARATOR}"
            ),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        growth_lines = [
            embed_builder.delta_line("Total", stats.total, baseline.total if baseline else None),
            embed_builder.delta_line("Members", stats.humans, baseline.humans if baseline else None),
            embed_builder.delta_line("Bots", stats.bots, baseline.bots if baseline else None),
        ]
        if self.presence_available:
            growth_lines.append(
                embed_builder.delta_line(
                    "Online", stats.online, baseline.online if baseline else None
                )
            )
        embed.add_field(
            name=embed_builder.section(f"Membership Growth ({days}d)"),
            value=embed_builder.code_block(growth_lines),
            inline=False,
        )

        peak = max((count for _, count in daily_joins), default=0)
        join_lines = []
        for day, count in daily_joins[-14:]:
            bar = "█" * round((count / peak) * 12) if peak else ""
            join_lines.append(f"{day:%d %b} │ {count:>3} {bar}")
        embed.add_field(
            name=embed_builder.section("New Members Per Day"),
            value=embed_builder.code_block(join_lines),
            inline=False,
        )

        month_lines = [f"{month} │ {count:>4}" for month, count in monthly_joins]
        current_month = _utc_today().strftime("%Y-%m")
        this_month = next(
            (count for month, count in monthly_joins if month == current_month), 0
        )
        month_lines.append("")
        month_lines.append(
            f"This month to date : {this_month:>4}\n"
            f"Same time last mth : {prev_month_to_date:>4}"
        )
        embed.add_field(
            name=embed_builder.section("Joins Trend"),
            value=(
                f"{embed_builder.item(f'New members in this window: **{window_joins:,}**')}\n"
                f"{embed_builder.code_block(month_lines)}"
            ),
            inline=False,
        )

        if top_active:
            active_lines = [
                embed_builder.item(
                    f"**{rank}.** {member.display_name} — **{member.points:,}** points"
                )
                for rank, member in enumerate(top_active, start=1)
            ]
        else:
            active_lines = [
                embed_builder.item("No tracked activity in this window yet.")
            ]
        embed.add_field(
            name=embed_builder.section(f"Most Active Members ({days}d)"),
            value="\n".join(active_lines),
            inline=False,
        )

        embed.add_field(
            name=embed_builder.section("Server Snapshot"),
            value=(
                f"{embed_builder.item(f'Members: **{stats.humans:,}**')}\n"
                f"{embed_builder.item(f'Bots: **{stats.bots:,}**')}\n"
                f"{embed_builder.item(f'Team / admins: **{stats.admins:,}**')}\n"
                + (
                    embed_builder.item(f"Online now: **{stats.online:,}**")
                    if self.presence_available
                    else embed_builder.item(
                        "Online now: *enable the Presence Intent to track this*"
                    )
                )
            ),
            inline=False,
        )
        return embed

    async def compose_report(
        self, guild: discord.Guild, *, days: int, label: str
    ) -> discord.Embed:
        """Recalculates every statistic and renders a fresh report embed."""
        stats = await self.record_snapshot(guild)
        baseline = await self.get_baseline(guild.id, days)
        daily_joins = await self.get_daily_joins(guild.id, max(days, 14))
        window_joins = await self.get_joins_in_window(guild.id, days)
        monthly_joins = await self.get_monthly_joins(guild.id, 4)
        prev_month = await self.get_prev_month_to_date(guild.id)
        top_active = await self.top_active_members(guild, days)
        return self.build_report_embed(
            guild=guild,
            label=label,
            days=days,
            stats=stats,
            baseline=baseline,
            daily_joins=daily_joins,
            window_joins=window_joins,
            monthly_joins=monthly_joins,
            prev_month_to_date=prev_month,
            top_active=top_active,
        )

    # -- deployed live reports ---------------------------------
    async def deploy_report(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        timeframe: str,
        days: int,
        label: str,
    ) -> discord.Message:
        """Posts a report and registers it for automatic refreshing."""
        await self.reconcile_joins_from_members(guild)
        embed = await self.compose_report(guild, days=days, label=label)
        message = await channel.send(embed=embed)
        await self.db.execute(
            """
            INSERT INTO member_reports
                (guild_id, timeframe, days, label, channel_id, message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id, timeframe) DO UPDATE SET
                days = excluded.days,
                label = excluded.label,
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                updated_at = datetime('now')
            """,
            (guild.id, timeframe, days, label, channel.id, message.id),
        )
        logger.info(
            "Deployed %s report for guild %s in channel %s", timeframe, guild.id, channel.id
        )
        return message

    async def list_reports(self, guild_id: int) -> list[dict]:
        rows = await self.db.fetchall(
            """
            SELECT timeframe, days, label, channel_id, message_id, updated_at
            FROM member_reports WHERE guild_id = ?
            ORDER BY days
            """,
            (guild_id,),
        )
        return [dict(row) for row in rows]

    async def remove_report(self, guild_id: int, timeframe: str) -> bool:
        row = await self.db.fetchone(
            "SELECT timeframe FROM member_reports WHERE guild_id = ? AND timeframe = ?",
            (guild_id, timeframe),
        )
        if row is None:
            return False
        await self.db.execute(
            "DELETE FROM member_reports WHERE guild_id = ? AND timeframe = ?",
            (guild_id, timeframe),
        )
        return True

    async def refresh_report_row(self, row: dict) -> bool:
        """Re-renders one deployed report in place."""
        guild = self.bot.get_guild(row["guild_id"]) if "guild_id" in row else None
        if guild is None:
            return False
        channel = guild.get_channel_or_thread(row["channel_id"])
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(row["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Live %s report message is gone; removing it from the schedule",
                row["timeframe"],
            )
            await self.remove_report(guild.id, row["timeframe"])
            return False

        embed = await self.compose_report(
            guild, days=row["days"], label=row["label"] or row["timeframe"].title()
        )
        await message.edit(embed=embed)
        await self.db.execute(
            """
            UPDATE member_reports SET updated_at = datetime('now')
            WHERE guild_id = ? AND timeframe = ?
            """,
            (guild.id, row["timeframe"]),
        )
        return True

    async def refresh_all_reports(self, guild: discord.Guild | None = None) -> int:
        if guild is None:
            rows = await self.db.fetchall(
                """
                SELECT guild_id, timeframe, days, label, channel_id, message_id
                FROM member_reports
                """
            )
        else:
            rows = await self.db.fetchall(
                """
                SELECT guild_id, timeframe, days, label, channel_id, message_id
                FROM member_reports WHERE guild_id = ?
                """,
                (guild.id,),
            )

        refreshed = 0
        for row in rows:
            try:
                if await self.refresh_report_row(dict(row)):
                    refreshed += 1
            except discord.HTTPException:
                logger.exception("Could not refresh a live community report")
        return refreshed

    # -- scheduled jobs ----------------------------------------
    @tasks.loop(time=dtime(hour=0, minute=5, tzinfo=timezone.utc))
    async def daily_snapshot_job(self) -> None:
        for guild in self.bot.guilds:
            await self.record_snapshot(guild)
            await self.reconcile_joins_from_members(guild)
        logger.info("Recorded daily growth baselines for %d server(s)", len(self.bot.guilds))

    @tasks.loop(hours=REPORT_REFRESH_HOURS)
    async def refresh_reports_job(self) -> None:
        for guild in self.bot.guilds:
            await self.record_snapshot(guild)
            await self.reconcile_joins_from_members(guild)
        refreshed = await self.refresh_all_reports()
        if refreshed:
            logger.info("Refreshed %d live community report(s)", refreshed)

    @daily_snapshot_job.before_loop
    @refresh_reports_job.before_loop
    async def before_jobs(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberReportCog(bot))
