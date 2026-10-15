"""
Unit tests for the onboarding engine and the welcome composer.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import patch

import discord

import cogs.onboarding as onboarding
from cogs.onboarding import (
    DEFAULT_MESSAGE,
    MESSAGE_SETTING,
    DayGroup,
    OnboardingCog,
    OnboardingSettings,
    _bucket_for,
    _chunk_mentions,
    missing_permissions,
)
from views.onboarding_view import (
    OnboardingSettingsView,
    WelcomeComposerView,
    WelcomeDraft,
    build_composer_embeds,
    build_settings_embed,
)

DEFAULT_CHANNEL = 111
OTHER_CHANNEL = 222


def run(coroutine):
    return asyncio.run(coroutine)


class FakeMember:
    def __init__(self, member_id, name, days_ago=0, dm_open=True):
        self.id = member_id
        self.display_name = name
        self.mention = f"<@{member_id}>"
        self.bot = False
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)
        self.dm_open = dm_open
        self.guild = SimpleNamespace(id=1, name="SafeRest", icon=None)
        self.sent = []

    async def send(self, **kwargs):
        if not self.dm_open:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="closed"), "closed")
        self.sent.append(kwargs)


class FakeDatabase:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.marked = []

    async def get_settings(self, guild_id, keys):
        return {key: self.settings.get(key) for key in keys}

    async def set_setting(self, guild_id, key, value):
        self.settings[key] = value

    async def executemany(self, query, rows):
        self.marked.extend(rows)


def make_cog(settings=None):
    cog = OnboardingCog.__new__(OnboardingCog)
    cog.bot = None
    cog.db = FakeDatabase(settings)
    return cog


def make_settings(default_channel=DEFAULT_CHANNEL, message=DEFAULT_MESSAGE, custom=False):
    return OnboardingSettings(
        channels={"welcome": default_channel, "onboarding": 5, "introductions": 6},
        sources={"welcome": "saved", "onboarding": "saved", "introductions": "env"},
        message=message,
        custom_message=custom,
    )


def make_groups():
    return [
        DayGroup("d0", "Joined today", "", [FakeMember(1, "Ana"), FakeMember(2, "Ben")]),
        DayGroup("d1", "Joined yesterday", "", [FakeMember(3, "Cy", days_ago=1)]),
    ]


def make_draft(**settings_kwargs):
    return WelcomeDraft.start(99, make_groups(), make_settings(**settings_kwargs))


class HelperTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(_bucket_for(0)[0], "d0")
        self.assertEqual(_bucket_for(1)[0], "d1")
        self.assertEqual(_bucket_for(6)[0], "d6")
        self.assertEqual(_bucket_for(10)[0], "w1")
        self.assertEqual(_bucket_for(40)[0], "w3")

    def test_chunk_mentions_respects_budget(self):
        mentions = [f"<@{index:018d}>" for index in range(200)]
        chunks = _chunk_mentions(mentions, 100, 500)
        self.assertLessEqual(len(chunks[0]), 100)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual(sum(len(chunk.split()) for chunk in chunks), 200)

    def test_missing_permissions(self):
        channel = SimpleNamespace(
            permissions_for=lambda member: SimpleNamespace(
                view_channel=True, send_messages=False
            )
        )
        self.assertEqual(missing_permissions(channel, None), ["Send Messages"])


class SettingsTests(unittest.TestCase):
    def test_saved_values_win_over_env(self):
        cog = make_cog({"welcome_channel_id": str(OTHER_CHANNEL)})
        with patch.dict(onboarding.ENV_CHANNEL_DEFAULTS, {"welcome": 1, "onboarding": 2, "introductions": None}):
            settings = run(cog.settings(1))
        self.assertEqual(settings.default_channel_id, OTHER_CHANNEL)
        self.assertEqual(settings.sources["welcome"], "saved")
        self.assertEqual(settings.sources["onboarding"], "env")
        self.assertEqual(settings.sources["introductions"], "unset")
        self.assertFalse(settings.custom_message)
        self.assertEqual(settings.message, DEFAULT_MESSAGE)

    def test_saved_message(self):
        cog = make_cog()
        run(cog.save_message(1, "  Hello {server_name}!  "))
        settings = run(cog.settings(1))
        self.assertTrue(settings.custom_message)
        self.assertEqual(settings.message, "Hello {server_name}!")

    def test_saving_built_in_message_clears_it(self):
        cog = make_cog({MESSAGE_SETTING: "custom"})
        run(cog.save_message(1, DEFAULT_MESSAGE))
        self.assertIsNone(cog.db.settings[MESSAGE_SETTING])

    def test_default_channel_can_be_set_and_cleared(self):
        cog = make_cog()
        run(cog.set_channel(1, "welcome", OTHER_CHANNEL))
        with patch.dict(onboarding.ENV_CHANNEL_DEFAULTS, {"welcome": None}):
            self.assertEqual(run(cog.settings(1)).default_channel_id, OTHER_CHANNEL)
            run(cog.set_channel(1, "welcome", None))
            self.assertIsNone(run(cog.settings(1)).default_channel_id)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(onboarding, "DM_PACE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cog = make_cog()
        self.cog.build_welcome_dm = lambda member, channels: "dm"
        self.guild = SimpleNamespace(id=1)
        self.moderator = SimpleNamespace(id=7)

    def deliver(self, members, mode, channel=None):
        return run(
            self.cog.deliver(
                guild=self.guild,
                members=members,
                message="hi",
                moderator=self.moderator,
                channel=channel,
                mode=mode,
                channels={},
            )
        )

    def test_dm_only_logs_only_delivered_members(self):
        members = [FakeMember(1, "Ana"), FakeMember(2, "Ben", dm_open=False)]
        result = self.deliver(members, "dm")
        self.assertEqual((result.dm_success, result.dm_failed), (1, 1))
        self.assertEqual(result.failed_names, ["Ben"])
        self.assertEqual([row[0] for row in self.cog.db.marked], [1])

    def test_post_logs_everyone(self):
        members = [FakeMember(1, "Ana"), FakeMember(2, "Ben", dm_open=False)]
        posted = []

        async def fake_post(channel, tagged, message, channels):
            posted.append((channel, [m.id for m in tagged]))
            return SimpleNamespace(jump_url="https://discord.com/x")

        self.cog.post_welcome = fake_post
        result = self.deliver(members, "post_dm", channel="chan")
        self.assertEqual(posted, [("chan", [1, 2])])
        self.assertEqual(result.jump_url, "https://discord.com/x")
        self.assertEqual(sorted(row[0] for row in self.cog.db.marked), [1, 2])

    def test_post_requires_channel(self):
        with self.assertRaises(ValueError):
            self.deliver([FakeMember(1, "Ana")], "post")
        self.assertEqual(self.cog.db.marked, [])


class DraftTests(unittest.TestCase):
    def test_starts_on_default_channel_and_saved_message(self):
        draft = make_draft(message="Saved!", custom=True)
        self.assertEqual(draft.channel_id, DEFAULT_CHANNEL)
        self.assertTrue(draft.is_default_channel)
        self.assertEqual(draft.message, "Saved!")
        self.assertEqual(len(draft.selected_members), 3)

    def test_day_selection(self):
        draft = make_draft()
        draft.apply_selection({"d1"})
        self.assertEqual([m.id for m in draft.selected_members], [3])
        draft.apply_selection({"d1", "all"})
        self.assertEqual(draft.selected_keys, {"all"})
        draft.apply_selection(set())
        self.assertEqual(len(draft.selected_members), 3)

    def test_choosing_other_channel_is_not_default(self):
        draft = make_draft()
        draft.channel_id = OTHER_CHANNEL
        self.assertFalse(draft.is_default_channel)

    def test_embeds_show_destination(self):
        draft = make_draft()
        control, preview = build_composer_embeds(draft, None)
        self.assertIn(f"<#{DEFAULT_CHANNEL}>", control.description)
        self.assertIn("default", control.description)
        self.assertIn("@Ana", preview.description)
        self.assertLessEqual(len(control.description), 4096)
        self.assertLessEqual(len(preview.description), 4096)

    def test_embeds_warn_without_channel(self):
        draft = make_draft(default_channel=None)
        control, _ = build_composer_embeds(draft, None)
        self.assertIn("No channel chosen", control.description)

    def test_dm_mode_has_no_post_preview(self):
        draft = make_draft()
        draft.mode = "dm"
        control, preview = build_composer_embeds(draft, None)
        self.assertNotIn("Posting in", control.description)
        self.assertIn("No public post", preview.description)


class ComposerViewTests(unittest.TestCase):
    def test_send_ready_with_default_channel(self):
        view = WelcomeComposerView(make_draft())
        self.assertFalse(view.send.disabled)
        self.assertEqual(view.send.label, "Send to 3 members")
        # Already the default, so there is nothing to make default.
        self.assertTrue(view.make_default.disabled)
        self.assertTrue(view.save_message.disabled)

    def test_channel_menu_preselects_destination(self):
        view = WelcomeComposerView(make_draft())
        channel_menu = next(
            item for item in view.children if isinstance(item, discord.ui.ChannelSelect)
        )
        self.assertEqual([value.id for value in channel_menu.default_values], [DEFAULT_CHANNEL])
        self.assertFalse(channel_menu.disabled)

    def test_send_blocked_without_channel(self):
        view = WelcomeComposerView(make_draft(default_channel=None))
        self.assertTrue(view.send.disabled)

    def test_dm_only_does_not_need_channel(self):
        draft = make_draft(default_channel=None)
        draft.mode = "dm"
        view = WelcomeComposerView(draft)
        self.assertFalse(view.send.disabled)
        channel_menu = next(
            item for item in view.children if isinstance(item, discord.ui.ChannelSelect)
        )
        self.assertTrue(channel_menu.disabled)

    def test_other_channel_can_become_default(self):
        draft = make_draft()
        draft.channel_id = OTHER_CHANNEL
        draft.message = "Edited"
        view = WelcomeComposerView(draft)
        self.assertFalse(view.make_default.disabled)
        self.assertFalse(view.save_message.disabled)
        self.assertFalse(view.revert_message.disabled)

    def test_component_layout_fits_discord(self):
        view = WelcomeComposerView(make_draft())
        rows = {}
        for item in view.children:
            rows.setdefault(item.row, []).append(item)
        self.assertLessEqual(len(rows), 5)


class SettingsViewTests(unittest.TestCase):
    def test_settings_screen(self):
        settings = make_settings()
        embed = build_settings_embed(settings, "Saved.")
        self.assertIn("Default welcome channel", embed.description)
        self.assertIn(f"<#{DEFAULT_CHANNEL}>", embed.description)
        self.assertIn("(from .env)", embed.description)
        view = OnboardingSettingsView(settings)
        self.assertTrue(view.reset_message.disabled)
        menus = [item for item in view.children if isinstance(item, discord.ui.ChannelSelect)]
        self.assertEqual(len(menus), 3)
        self.assertTrue(all(menu.min_values == 0 for menu in menus))


if __name__ == "__main__":
    unittest.main()
