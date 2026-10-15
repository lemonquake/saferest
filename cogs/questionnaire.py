"""Member questionnaire with persistent buttons and a durable Slack delivery queue."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    BRAND_NAME, BRAND_COLOR, COMMUNITY_DATABASE_PATH, SLACK_ONBOARDING_WEBHOOK_URL,
)
from core.database import get_database
from core.questionnaire import valid_webhook, slack_payload
from views.questionnaire_view import MemberQuestionnaireView, questionnaire_embed
from views.common import require_manage

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS questionnaire_responses (
    submission_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at TEXT,
    UNIQUE(guild_id, user_id)
)
"""


class QuestionnaireCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = get_database(COMMUNITY_DATABASE_PATH)
        self.webhook = SLACK_ONBOARDING_WEBHOOK_URL
        self.delivery_lock = asyncio.Lock()
        self.view = MemberQuestionnaireView()
        self.session = None

    @property
    def configured(self):
        return valid_webhook(self.webhook)

    async def cog_load(self):
        await self.db.initialize()
        await self.db.execute(SCHEMA)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.bot.add_view(self.view)
        self.deliver_queue.start()

    async def cog_unload(self):
        self.deliver_queue.cancel()
        self.view.stop()
        if self.session:
            await self.session.close()

    async def existing(self, guild_id, user_id):
        return await self.db.fetchone(
            "SELECT status FROM questionnaire_responses WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def save_answers(self, interaction, answers):
        record = {
            "submission_id": str(interaction.id), "guild_id": interaction.guild_id,
            "user_id": interaction.user.id, "discord_account": str(interaction.user),
            "submitted_at": datetime.now(timezone.utc).isoformat(), "answers": answers,
        }
        result = await self.db.execute_write(
            "INSERT OR IGNORE INTO questionnaire_responses "
            "(submission_id, guild_id, user_id, payload) VALUES (?, ?, ?, ?)",
            (record["submission_id"], record["guild_id"], record["user_id"],
             json.dumps(slack_payload(BRAND_NAME, record))),
        )
        return bool(result.rowcount)

    async def post_copy(self, channel):
        """Posts a fresh copy of the questionnaire in the channel a moderator picked."""
        return await channel.send(embed=questionnaire_embed(), view=self.view,
                                  allowed_mentions=discord.AllowedMentions.none())

    async def flush_queue(self):
        if not self.configured:
            return
        async with self.delivery_lock:
            rows = await self.db.fetchall(
                "SELECT * FROM questionnaire_responses WHERE status = 'pending' "
                "AND retry_at <= ? ORDER BY rowid LIMIT 10", (time.time(),),
            )
            for row in rows:
                error = "network_error"
                retry_delay = min(3600, 30 * 2 ** min(row["attempts"], 7))
                blocked = False
                rate_limited = False
                try:
                    async with self.session.post(
                        self.webhook, json=json.loads(row["payload"]), allow_redirects=False,
                    ) as response:
                        body = (await response.text()).strip()
                        if response.status == 200 and body == "ok":
                            await self.db.execute(
                                "UPDATE questionnaire_responses SET status = 'sent', "
                                "sent_at = datetime('now'), last_error = NULL, attempts = attempts + 1 "
                                "WHERE submission_id = ?", (row["submission_id"],),
                            )
                            continue
                        error = f"slack_http_{response.status}"
                        rate_limited = response.status == 429
                        if rate_limited:
                            try:
                                retry_delay = max(30, float(response.headers.get("Retry-After", "60")))
                            except ValueError:
                                retry_delay = 60
                        blocked = 300 <= response.status < 500 and not rate_limited
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    # Never log an exception containing the secret webhook URL or answers.
                    pass
                await self.db.execute(
                    "UPDATE questionnaire_responses SET attempts = attempts + 1, "
                    "retry_at = ?, last_error = ?, status = ? WHERE submission_id = ?",
                    (time.time() + retry_delay, error, "blocked" if blocked else "pending", row["submission_id"]),
                )
                logger.warning("Slack onboarding delivery %s: %s", row["submission_id"], error)
                if rate_limited or blocked:
                    # Apply the cooldown to the whole destination, not just this response.
                    await self.db.execute(
                        "UPDATE questionnaire_responses SET retry_at = MAX(retry_at, ?) "
                        "WHERE status = 'pending'", (time.time() + max(retry_delay, 300 if blocked else 0),),
                    )
                    break

    @tasks.loop(seconds=30)
    async def deliver_queue(self):
        try:
            await self.flush_queue()
        except Exception as error:
            logger.error("Questionnaire delivery worker failed (%s)", type(error).__name__)

    @deliver_queue.before_loop
    async def before_delivery(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="post-questionnaire", description="Post the member questionnaire in any channel (leave empty for this one).")
    @app_commands.describe(channel="Where to post it. Leave empty to post in this channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def post_questionnaire(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await require_manage(interaction):
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Pick a text channel to post the questionnaire in.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        permissions = target.permissions_for(interaction.guild.me)
        missing = [label for attr, label in (("view_channel", "View Channel"), ("send_messages", "Send Messages"),
                                             ("embed_links", "Embed Links")) if not getattr(permissions, attr)]
        if missing:
            await interaction.followup.send(f"The bot needs **{', '.join(missing)}** in {target.mention}.", ephemeral=True)
            return
        try:
            message = await self.post_copy(target)
        except discord.HTTPException as error:
            logger.warning("Questionnaire post failed (%s)", type(error).__name__)
            await interaction.followup.send(f"Could not post the questionnaire in {target.mention}.", ephemeral=True)
            return
        status = f"Questionnaire posted in {target.mention} · [jump to it]({message.jump_url})"
        if not self.configured:
            status += "\nSlack is not connected yet; responses will be saved locally and queued."
        await interaction.followup.send(status, ephemeral=True)

    @app_commands.command(name="onboarding-status", description="Check questionnaire delivery to Slack; optionally retry failed deliveries.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def onboarding_status(self, interaction: discord.Interaction, retry_failed: bool = False):
        if not await require_manage(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if retry_failed:
            await self.db.execute(
                "UPDATE questionnaire_responses SET status = 'pending', retry_at = 0 "
                "WHERE guild_id = ? AND status = 'blocked'", (interaction.guild_id,),
            )
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS total FROM questionnaire_responses WHERE guild_id = ? GROUP BY status",
            (interaction.guild_id,),
        )
        counts = {row["status"]: row["total"] for row in rows}
        await interaction.followup.send(
            f"Slack connection: {'configured (delivery not verified)' if self.configured else 'not configured'}\n"
            f"Saved responses: {sum(counts.values())}\nSent to Slack: {counts.get('sent', 0)}\n"
            f"Waiting for Slack: {counts.get('pending', 0)}\nNeed attention: {counts.get('blocked', 0)}",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(QuestionnaireCog(bot))
