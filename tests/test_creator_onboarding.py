"""
Unit tests for creator onboarding (the TikTok Shop question).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio
import unittest
import unittest.mock

import discord

from cogs.creator_onboarding import (
    CHANNEL_SETTINGS,
    JOURNEY_QUESTION,
    NEXT_STEP_MESSAGE,
    NOT_A_CREATOR_MESSAGE,
    WELCOME_MESSAGE,
    CreatorOnboardingCog,
    CreatorSettings,
    completion_message,
    getting_started_mention,
    learning_message,
)
from cogs.onboarding import MAX_MESSAGE_LENGTH, DayGroup
from views.creator_onboarding_view import (
    CreatorComposerView,
    CreatorDraft,
    CreatorJourneyView,
    CreatorOnboardingHubView,
    CreatorQuestionnaireModal,
    CreatorQuestionnaireView,
    CreatorTestView,
    CreatorWelcomeView,
    Shop101DoneView,
    build_composer_embeds,
)
from views.mod_panel_view import ModPanelView


def run(coroutine):
    return asyncio.run(coroutine)


class FakeMember:
    def __init__(self, member_id, days_ago=0):
        self.id = member_id
        self.display_name = f"Member{member_id}"
        self.mention = f"<@{member_id}>"
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)


class FakeChannel:
    def __init__(self, channel_id=500):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(jump_url=f"https://discord.test/{len(self.sent)}")


class FakeDatabase:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.marked = []
        self.writes = []

    async def get_settings(self, guild_id, keys):
        return {key: self.settings.get(key) for key in keys}

    async def set_setting(self, guild_id, key, value):
        self.settings[key] = value

    async def executemany(self, query, rows):
        self.marked.extend(rows)

    async def execute(self, query, params=()):
        self.writes.append(params)


def make_cog(settings=None):
    cog = CreatorOnboardingCog.__new__(CreatorOnboardingCog)
    cog.bot = None
    cog.db = FakeDatabase(settings)
    return cog


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    def is_done(self):
        return bool(self.sent)


class FakeSafeRest:
    """Stands in for SafeRestOnboardingCog: says whose onboarding channel this is."""

    def __init__(self, owner=None, granted=True):
        self.owner, self.granted, self.completed = owner, granted, []

    async def owner_of(self, channel):
        return self.owner

    async def complete(self, member):
        self.completed.append(member.id)
        return self.granted


def make_interaction(cog, guild=None, saferest=None):
    cogs = {"CreatorOnboardingCog": cog, "SafeRestOnboardingCog": saferest}
    client = SimpleNamespace(get_cog=lambda name: cogs.get(name))
    return SimpleNamespace(
        client=client,
        guild=guild or SimpleNamespace(id=1, text_channels=[]),
        channel=FakeChannel(),
        channel_id=500,
        user=SimpleNamespace(id=42, bot=False),
        response=FakeResponse(),
    )


def click(view, custom_id, interaction):
    item = next(child for child in view.children if child.custom_id == custom_id)
    return run(item.callback(interaction))


class CopyTests(unittest.TestCase):
    def test_messages_match_the_script(self):
        self.assertIn("Welcome to the SafeRest Creator Community!", WELCOME_MESSAGE)
        self.assertIn("Are you a TikTok Shop creator?", WELCOME_MESSAGE)
        self.assertIn("[shop.tiktok.com](http://shop.tiktok.com)", NOT_A_CREATOR_MESSAGE)
        self.assertIn("at least 1,000 followers", NOT_A_CREATOR_MESSAGE)
        self.assertIn("Where are you in your TikTok Shop journey?", JOURNEY_QUESTION)

    def test_getting_started_channel_fallbacks(self):
        self.assertEqual(getting_started_mention(None, 77), "<#77>")
        named = SimpleNamespace(name="🛍️tiktok-shop-101", mention="<#88>")
        guild = SimpleNamespace(text_channels=[SimpleNamespace(name="general", mention="<#1>"), named])
        self.assertEqual(getting_started_mention(guild, None), "<#88>")
        self.assertEqual(getting_started_mention(SimpleNamespace(text_channels=[]), None), "**#🛍️tiktok-shop-101**")
        self.assertIn("Head to <#77> to get everything", learning_message(None, 77))
        self.assertEqual(
            getting_started_mention(SimpleNamespace(id=1377020906441932800), 1557490449160085615),
            "[#🛍️tiktok-shop-101](https://discord.com/channels/1377020906441932800/1557490449160085615)",
        )


class SettingsTests(unittest.TestCase):
    def test_saved_channel_and_unset(self):
        cog = make_cog({CHANNEL_SETTINGS["welcome"]: "123"})
        settings = run(cog.settings(1))
        self.assertEqual(settings.default_channel_id, 123)
        self.assertEqual(settings.sources["welcome"], "saved")
        # Not saved and SafeRest ships no built-in default, so it is unset.
        self.assertIsNone(settings.channels["getting_started"])
        self.assertEqual(settings.sources["getting_started"], "unset")

    def test_set_and_clear_channel(self):
        cog = make_cog()
        run(cog.set_channel(1, "getting_started", 99))
        self.assertEqual(run(cog.settings(1)).channels["getting_started"], 99)
        run(cog.set_channel(1, "getting_started", None))
        self.assertIsNone(run(cog.settings(1)).channels["getting_started"])


class PostTests(unittest.TestCase):
    def test_single_message_carries_tags_question_and_buttons(self):
        cog = make_cog()
        channel = FakeChannel()
        view = object()
        run(cog.post_welcome(channel, [FakeMember(1), FakeMember(2)], view))
        self.assertEqual(len(channel.sent), 1)
        content = channel.sent[0]["content"]
        self.assertTrue(content.startswith("<@1> <@2>"))
        self.assertTrue(content.endswith(WELCOME_MESSAGE))
        self.assertIs(channel.sent[0]["view"], view)

    def test_test_message_has_no_tags(self):
        channel = FakeChannel()
        run(make_cog().post_welcome(channel, [], object()))
        self.assertEqual(channel.sent[0]["content"], WELCOME_MESSAGE)

    def test_many_members_put_buttons_on_the_last_message(self):
        channel = FakeChannel()
        view = object()
        members = [FakeMember(10**17 + i) for i in range(200)]
        run(make_cog().post_welcome(channel, members, view))
        self.assertGreater(len(channel.sent), 1)
        self.assertTrue(all("view" not in sent for sent in channel.sent[:-1]))
        self.assertIs(channel.sent[-1]["view"], view)
        self.assertEqual(channel.sent[-1]["content"], WELCOME_MESSAGE)
        self.assertTrue(all(len(sent["content"]) <= MAX_MESSAGE_LENGTH for sent in channel.sent))
        tagged = " ".join(sent["content"] for sent in channel.sent[:-1])
        self.assertTrue(all(member.mention in tagged for member in members))

    def test_deliver_logs_everyone_tagged(self):
        cog = make_cog()
        members = [FakeMember(1), FakeMember(2)]
        _, marked = run(cog.deliver(
            guild=SimpleNamespace(id=1), members=members,
            moderator=SimpleNamespace(id=9), channel=FakeChannel(), view=object(),
        ))
        self.assertEqual(marked, 2)
        self.assertEqual(cog.db.marked, [(1, 1, 9), (2, 1, 9)])


class MemberButtonTests(unittest.TestCase):
    def test_no_sends_private_guide(self):
        cog = make_cog()
        interaction = make_interaction(cog)
        click(CreatorWelcomeView(), "saferest_creator_onboarding:no", interaction)
        content, kwargs = interaction.response.sent[0]
        self.assertEqual(content, NOT_A_CREATOR_MESSAGE)
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(cog.db.writes, [(42, 1, 0, None)])

    def test_yes_asks_about_the_journey(self):
        cog = make_cog()
        interaction = make_interaction(cog)
        click(CreatorWelcomeView(), "saferest_creator_onboarding:yes", interaction)
        content, kwargs = interaction.response.sent[0]
        self.assertEqual(content, JOURNEY_QUESTION)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"], CreatorJourneyView)
        self.assertEqual(cog.db.writes, [(42, 1, 1, None)])

    def test_learning_points_to_saved_channel(self):
        cog = make_cog({CHANNEL_SETTINGS["getting_started"]: "555"})
        interaction = make_interaction(cog)
        click(CreatorJourneyView(), "saferest_creator_onboarding:learning", interaction)
        content, kwargs = interaction.response.sent[0]
        self.assertIn("Head to [#🛍️tiktok-shop-101](https://discord.com/channels/1/555)", content)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"], Shop101DoneView)
        self.assertEqual(cog.db.writes, [(42, 1, 1, "learning")])

    def test_experienced_gets_rules_and_questionnaire(self):
        cog = make_cog()
        interaction = make_interaction(cog)
        click(CreatorJourneyView(), "saferest_creator_onboarding:experienced", interaction)
        content, kwargs = interaction.response.sent[0]
        self.assertEqual(content, NEXT_STEP_MESSAGE)
        self.assertIn("Start onboarding", kwargs["embed"].description)
        self.assertIsInstance(kwargs["view"], CreatorQuestionnaireView)
        self.assertEqual(cog.db.writes, [(42, 1, 1, "experienced")])

    def test_finishing_shop_101_shows_the_next_step(self):
        interaction = make_interaction(make_cog())
        click(Shop101DoneView(), "saferest_creator_onboarding:finished_101", interaction)
        content, kwargs = interaction.response.sent[0]
        self.assertEqual(content, NEXT_STEP_MESSAGE)
        self.assertIsInstance(kwargs["view"], CreatorQuestionnaireView)

    def test_rules_copy(self):
        self.assertIn("## Ready to start earning with SafeRest? 💰", NEXT_STEP_MESSAGE)
        self.assertIn(
            "[support ticket channel](https://discord.com/channels/1546233896918720645/1558164502091137024)",
            NEXT_STEP_MESSAGE,
        )
        self.assertIn(
            "[product info](https://discord.com/channels/1546233896918720645/1557841878064300072)",
            NEXT_STEP_MESSAGE,
        )
        self.assertTrue(NEXT_STEP_MESSAGE.endswith("## Complete Your Onboarding 👇"))
        self.assertLess(len(NEXT_STEP_MESSAGE), 2000)

    def test_reply_still_sent_when_cog_missing(self):
        interaction = make_interaction(None)
        click(CreatorWelcomeView(), "saferest_creator_onboarding:yes", interaction)
        click(CreatorJourneyView(), "saferest_creator_onboarding:learning", interaction)
        self.assertIn("**#🛍️tiktok-shop-101**", interaction.response.sent[1][0])


class TicketModeTests(unittest.TestCase):
    """Inside a member's own SafeRest Onboarding channel."""

    def test_replies_stay_in_the_channel(self):
        cog = make_cog()
        interaction = make_interaction(cog, saferest=FakeSafeRest(owner=42))
        click(CreatorWelcomeView(), "saferest_creator_onboarding:yes", interaction)
        content, kwargs = interaction.response.sent[0]
        self.assertEqual(content, JOURNEY_QUESTION)
        self.assertFalse(kwargs["ephemeral"])
        self.assertEqual(cog.db.writes, [(42, 1, 1, None)])

    def test_someone_else_cannot_answer(self):
        cog = make_cog()
        interaction = make_interaction(cog, saferest=FakeSafeRest(owner=99))
        click(CreatorWelcomeView(), "saferest_creator_onboarding:no", interaction)
        content, kwargs = interaction.response.sent[0]
        self.assertIn("<@99>", content)
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(cog.db.writes, [])

    def test_questionnaire_opens_only_for_the_owner(self):
        modals = []

        async def send_modal(modal):
            modals.append(modal)

        interaction = make_interaction(make_cog(), saferest=FakeSafeRest(owner=42))
        interaction.response.send_modal = send_modal
        click(CreatorQuestionnaireView(), "saferest_creator_onboarding:questionnaire", interaction)
        self.assertIsInstance(modals[0], CreatorQuestionnaireModal)
        self.assertTrue(modals[0].in_ticket)

        other = make_interaction(make_cog(), saferest=FakeSafeRest(owner=99))
        other.response.send_modal = send_modal
        click(CreatorQuestionnaireView(), "saferest_creator_onboarding:questionnaire", other)
        self.assertEqual(len(modals), 1)
        self.assertIn("<@99>", other.response.sent[0][0])


class FakeGuildMember(discord.Object):
    mention = "<@42>"


FULL_ACCESS = (
    "You now have full access to the SafeRest creator community! Head to "
    "[#opportunities-to-earn](https://discord.com/channels/1/1560318417922428948) "
    "to see all the ways you can earn with us. 🎉"
)


class QuestionnaireCompletionTests(unittest.TestCase):
    def finish(self, *, in_ticket, granted, saved=True, settings=None, channel_fails=False):
        saferest = FakeSafeRest(owner=42 if in_ticket else None, granted=granted)
        interaction = make_interaction(make_cog(settings), saferest=saferest)
        interaction.user = FakeGuildMember(42)
        followups, deleted = [], []

        async def followup(content, **kwargs):
            followups.append((content, kwargs))

        async def channel_send(content=None, **kwargs):
            if channel_fails:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
            interaction.channel.sent.append({"content": content, **kwargs})

        async def delete_original_response():
            deleted.append(True)

        interaction.followup = SimpleNamespace(send=followup)
        interaction.channel.send = channel_send
        interaction.delete_original_response = delete_original_response
        modal = CreatorQuestionnaireModal(SimpleNamespace(id=42), in_ticket=in_ticket)
        with unittest.mock.patch.object(discord, "Member", FakeGuildMember):
            run(modal.after_save(interaction, saved))
        self.deleted = deleted
        return saferest, interaction, followups

    def test_completion_message_copy(self):
        guild = SimpleNamespace(id=1377020906441932800)
        self.assertEqual(
            completion_message(guild, 1557773483243741226),
            "You now have full access to the SafeRest creator community! Head to "
            "[#opportunities-to-earn](https://discord.com/channels/1377020906441932800/1557773483243741226) "
            "to see all the ways you can earn with us. 🎉",
        )
        self.assertIn("**#opportunities-to-earn**", completion_message(guild, None))

    def test_ticket_gets_full_access_message_after_the_role(self):
        saferest, interaction, followups = self.finish(in_ticket=True, granted=True)
        self.assertEqual(saferest.completed, [42])  # role granted
        self.assertEqual(interaction.channel.sent[0]["content"], FULL_ACCESS)
        self.assertEqual(followups, [])  # no duplicate private copy
        self.assertEqual(self.deleted, [True])  # the "thinking…" note is removed

    def test_shared_channel_gets_it_privately(self):
        _, interaction, followups = self.finish(in_ticket=False, granted=True, saved=False)
        self.assertEqual(followups[0], (FULL_ACCESS, {"ephemeral": True}))
        self.assertEqual(interaction.channel.sent, [])

    def test_saved_opportunities_channel_is_used(self):
        _, _, followups = self.finish(
            in_ticket=False, granted=True, settings={CHANNEL_SETTINGS["opportunities"]: "999"}
        )
        self.assertIn("https://discord.com/channels/1/999", followups[0][0])

    def test_role_failure_is_reported(self):
        _, interaction, followups = self.finish(in_ticket=True, granted=False)
        post = interaction.channel.sent[0]["content"]
        self.assertIn("our team will give you access shortly", post)
        self.assertIn("could not be granted to <@42>", post)
        self.assertNotIn("full access", post)

    def test_falls_back_to_private_reply_if_channel_post_fails(self):
        _, _, followups = self.finish(in_ticket=True, granted=True, channel_fails=True)
        self.assertEqual(followups[0], (FULL_ACCESS, {"ephemeral": True}))
        self.assertEqual(self.deleted, [])

    def test_edit_answers_keeps_ticket_mode(self):
        modal = CreatorQuestionnaireModal(SimpleNamespace(id=42), in_ticket=True)
        again = modal.reopen(SimpleNamespace(id=42), {"email": "x"})
        self.assertIsInstance(again, CreatorQuestionnaireModal)
        self.assertTrue(again.in_ticket)


class ModeratorViewTests(unittest.TestCase):
    def make_draft(self, problems=None):
        groups = [
            DayGroup("d0", "Joined today", "", [FakeMember(1)]),
            DayGroup("d1", "Joined yesterday", "", [FakeMember(2, days_ago=1)]),
        ]
        settings = CreatorSettings(
            channels={"welcome": None, "getting_started": None},
            sources={"welcome": "unset", "getting_started": "unset"},
        )
        return CreatorDraft.start(
            7, groups, settings, category="**Onboarding**", problems=problems
        )

    def test_composer_opens_private_channels(self):
        async def check():
            draft = self.make_draft()
            view = CreatorComposerView(draft)
            self.assertEqual(view.send.label, "Send to 2 members")
            self.assertFalse(view.send.disabled)
            embeds = build_composer_embeds(draft)
            self.assertIn("#handle-onboarding", embeds[0].description)
            self.assertIn("**Onboarding**", embeds[0].description)
            self.assertIn("Are you a TikTok Shop creator?", embeds[1].description)
            self.assertIn("private onboarding channel", embeds[1].description)
            draft.apply_selection({"d1"})
            self.assertEqual([m.id for m in draft.selected_members], [2])

        run(check())

    def test_composer_blocked_by_setup_problems(self):
        async def check():
            draft = self.make_draft(problems=["The onboarding **category** was not found."])
            self.assertTrue(CreatorComposerView(draft).send.disabled)
            self.assertIn("Fix before sending", build_composer_embeds(draft)[0].description)

        run(check())

    def test_test_view_needs_a_channel(self):
        async def check():
            self.assertTrue(CreatorTestView().send_test.disabled)
            self.assertFalse(CreatorTestView(500).send_test.disabled)

        run(check())

    def test_persistent_views_and_panel_buttons(self):
        async def check():
            for view in (CreatorOnboardingHubView(), CreatorWelcomeView(), CreatorJourneyView()):
                self.assertTrue(view.is_persistent())
            for view in (Shop101DoneView(), CreatorQuestionnaireView()):
                self.assertTrue(view.is_persistent())
            panel = {child.custom_id: child.label for child in ModPanelView().children}
            self.assertEqual(panel["saferest_mod_panel:saferest_onboarding"], "SafeRest Onboarding")
            self.assertEqual(panel["saferest_mod_panel:creator_onboarding"], "Onboarding")
            self.assertEqual(panel["saferest_mod_panel:onboarding"], "Information Onboarding")
            self.assertLessEqual(
                sum(1 for child in ModPanelView().children if child.row == 0), 5
            )

        run(check())


if __name__ == "__main__":
    unittest.main()
