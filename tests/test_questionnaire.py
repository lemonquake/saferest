"""Questionnaire persistence, privacy, permissions and Slack failure handling."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from core.database import CommunityDatabase
from core.questionnaire import slack_payload, valid_webhook, validate_answers
from cogs.questionnaire import QuestionnaireCog, SCHEMA
from views.onboarding_view import QuestionnairePostView, open_questionnaire_poster
from views.questionnaire_view import MemberQuestionnaireView, QuestionnaireModal


ANSWERS = dict(tt_handle="@creator", email="creator@example.com",
               gmv="USD 5,000", birthday="02/29", phone="+1 (555) 123-4567")


class Response:
    def __init__(self, status=200, text="ok", headers=None):
        self.status, self.body, self.headers = status, text, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.body


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ValidationTests(unittest.TestCase):
    def test_email_and_birthday(self):
        self.assertEqual(validate_answers(ANSWERS)["birthday"], "02/29")
        self.assertEqual(validate_answers({**ANSWERS, "birthday": ""})["birthday"], "")
        for bad in [{"email": "not-an-email"}, {"birthday": "02/30"}, {"birthday": "2020-01-01"}, {"gmv": " "}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_answers({**ANSWERS, **bad})

    def test_phone_is_required_and_checked(self):
        self.assertEqual(validate_answers(ANSWERS)["phone"], "+1 (555) 123-4567")
        self.assertEqual(validate_answers({**ANSWERS, "phone": " 0917 123 4567 "})["phone"], "0917 123 4567")
        for bad in ["", "  ", "call me", "12345", "+1 555 123 4567 ext 9", "1234567890123456"]:
            with self.subTest(phone=bad), self.assertRaises(ValueError):
                validate_answers({**ANSWERS, "phone": bad})

    def test_slack_message_lists_phone_after_email(self):
        record = dict(answers=ANSWERS, discord_account="actual", guild_id=1, user_id=2,
                      submission_id="3", submitted_at="now")
        labels = [b["text"]["text"].split("\n")[0] for b in slack_payload("Test Brand", record)["blocks"][1:]]
        self.assertEqual(labels[:3], ["TikTok handle", "Email", "Phone number"])
        self.assertNotIn("Discord handle (provided)", labels)

    def test_slack_urls_only(self):
        self.assertTrue(valid_webhook("https://hooks.slack.com/services/TTEST/BTEST/SECRET"))
        for url in ["", "https://[invalid", "http://hooks.slack.com/services/T/B/S", "https://example.com/services/T/B/S",
                    "https://hooks.slack.com.evil.test/services/T/B/S", "https://hooks.slack.com/services/T/B/S?redirect=1"]:
            self.assertFalse(valid_webhook(url))

    def test_untrusted_answers_are_plain_text(self):
        record = dict(answers={**ANSWERS, "tt_handle": "<!channel> <https://evil.test|click>"},
                      discord_account="actual", guild_id=1, user_id=2,
                      submission_id="3", submitted_at="now")
        payload = slack_payload("Test Brand", record)
        self.assertNotIn("<!channel>", payload["text"])
        self.assertTrue(all(b["text"]["type"] == "plain_text" for b in payload["blocks"]))
        self.assertIn("Discord user ID\n2", json.dumps(payload).replace("\\n", "\n"))


class QuestionnaireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.db = CommunityDatabase(Path(self.temp.name) / "community.sqlite3")
        await self.db.initialize()
        await self.db.execute(SCHEMA)
        self.cog = QuestionnaireCog.__new__(QuestionnaireCog)
        self.cog.db = self.db
        self.cog.webhook = "https://hooks.slack.com/services/TTEST/BTEST/SECRET"
        self.cog.delivery_lock = asyncio.Lock()
        self.cog.view = MemberQuestionnaireView()
        self.cog.session = Session(Response())
        self.interaction = SimpleNamespace(id=3, guild_id=1, user=SimpleNamespace(id=2))

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def row(self):
        return await self.db.fetchone("SELECT * FROM questionnaire_responses")

    async def test_unique_response_and_delivery(self):
        self.assertTrue(await self.cog.save_answers(self.interaction, ANSWERS))
        self.interaction.id = 4
        self.assertFalse(await self.cog.save_answers(self.interaction, ANSWERS))
        await asyncio.gather(self.cog.flush_queue(), self.cog.flush_queue())
        self.assertEqual(len(self.cog.session.calls), 1)
        self.assertEqual((await self.row())["status"], "sent")
        self.assertFalse(self.cog.session.calls[0][1]["allow_redirects"])

    async def test_restart_preserves_pending_submission(self):
        await self.cog.save_answers(self.interaction, ANSWERS)
        await self.db.close()
        await self.db.initialize()
        await self.cog.flush_queue()
        self.assertEqual((await self.row())["status"], "sent")

    async def test_missing_connection_keeps_answers(self):
        self.cog.webhook = ""
        await self.cog.save_answers(self.interaction, ANSWERS)
        await self.cog.flush_queue()
        self.assertEqual((await self.row())["status"], "pending")
        self.assertEqual(self.cog.session.calls, [])

    async def test_network_failure_retries_without_logging_secret(self):
        await self.cog.save_answers(self.interaction, ANSWERS)
        self.cog.session = Session(asyncio.TimeoutError("SECRET"))
        with self.assertLogs("cogs.questionnaire", level="WARNING") as captured:
            await self.cog.flush_queue()
        self.assertNotIn("SECRET", str(captured.output))
        row = await self.row()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["retry_at"], 0)

    async def test_rate_limit_cools_down_whole_queue(self):
        await self.cog.save_answers(self.interaction, ANSWERS)
        self.interaction.id, self.interaction.user.id = 4, 5
        await self.cog.save_answers(self.interaction, ANSWERS)
        self.cog.session = Session(Response(429, "rate_limited", {"Retry-After": "120"}))
        await self.cog.flush_queue()
        await self.cog.flush_queue()
        self.assertEqual(len(self.cog.session.calls), 1)
        rows = await self.db.fetchall("SELECT status, retry_at FROM questionnaire_responses")
        self.assertTrue(all(r["status"] == "pending" and r["retry_at"] > 0 for r in rows))

    async def test_invalid_webhook_blocks_until_admin_retry(self):
        await self.cog.save_answers(self.interaction, ANSWERS)
        self.cog.session = Session(Response(403, "invalid_token"))
        await self.cog.flush_queue()
        await self.cog.flush_queue()
        self.assertEqual(len(self.cog.session.calls), 1)
        self.assertEqual((await self.row())["status"], "blocked")

    async def test_form_has_four_required_fields_with_phone_after_email(self):
        modal = QuestionnaireModal(SimpleNamespace(id=2))
        self.assertEqual(len(modal.children), 5)
        self.assertEqual(sum(field.required for field in modal.children), 4)
        self.assertEqual(list(modal.inputs), ["tt_handle", "email", "phone", "gmv", "birthday"])
        self.assertFalse(modal.inputs["birthday"].required)
        self.assertTrue(modal.inputs["phone"].required)
        self.assertTrue(self.cog.view.is_persistent())

    async def test_form_opens_in_any_server_channel(self):
        interaction = SimpleNamespace(guild=object(), channel_id=0, user=SimpleNamespace(id=2, bot=False),
                                      response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()))
        await self.cog.view.start.callback(interaction)
        interaction.response.send_modal.assert_awaited_once()
        interaction.response.send_message.assert_not_called()

    async def test_form_cannot_open_outside_a_server(self):
        interaction = SimpleNamespace(guild=None, channel_id=0, user=SimpleNamespace(id=2, bot=False),
                                      response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()))
        await self.cog.view.start.callback(interaction)
        interaction.response.send_modal.assert_not_called()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

    async def test_post_copy_always_posts_a_new_message(self):
        channel = AsyncMock(spec=discord.TextChannel)
        await self.cog.post_copy(channel)
        await self.cog.post_copy(channel)
        self.assertEqual(channel.send.await_count, 2)
        self.assertIs(channel.send.call_args.kwargs["view"], self.cog.view)

    async def test_picker_posts_copies_to_each_chosen_channel(self):
        class Permissions:
            view_channel = send_messages = embed_links = True

        def make_channel(channel_id):
            channel = AsyncMock(spec=discord.TextChannel)
            channel.id, channel.mention = channel_id, f"<#{channel_id}>"
            channel.permissions_for = lambda member: Permissions()
            channel.send.return_value = SimpleNamespace(jump_url=f"https://discord.test/{channel_id}")
            return channel

        channels = {500: make_channel(500), 600: make_channel(600)}
        guild = SimpleNamespace(id=1, me=object(), get_channel_or_thread=channels.get)
        interaction = SimpleNamespace(
            guild=guild,
            client=SimpleNamespace(get_cog=lambda name: self.cog if name == "QuestionnaireCog" else None),
            response=SimpleNamespace(is_done=lambda: True),
            edit_original_response=AsyncMock(),
        )
        posted = []
        for channel_id in (500, 500, 600):
            await QuestionnairePostView(channel_id, posted).post.callback(interaction)
        self.assertEqual(channels[500].send.await_count, 2)
        self.assertEqual(channels[600].send.await_count, 1)
        self.assertEqual([mention for mention, _ in posted], ["<#500>", "<#500>", "<#600>"])
        embed = interaction.edit_original_response.call_args.kwargs["embeds"][0]
        self.assertIn("Posted this session", embed.description)
        self.assertTrue(QuestionnairePostView().post.disabled)

    async def test_poster_starts_without_a_channel(self):
        """No fixed channel: the moderator always chooses where it goes."""
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await open_questionnaire_poster(interaction)
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["view"].post.disabled)
        self.assertIn("pick a channel below", kwargs["embed"].description)

if __name__ == "__main__":
    unittest.main()
