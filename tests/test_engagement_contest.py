"""
Unit tests for the Engagement Contest feature.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from cogs.engagement_contest import (
    CHANNELS_SETTING,
    REPORT_CHANNEL_SETTING,
    ContestEvent,
    EngagementContestCog,
    ContestStore,
    StoredEvent,
    build_standings,
    contains_tiktok_link,
    content_fingerprint,
    extract_tiktok_links,
    is_contest_channel,
    is_excluded_member,
    is_genuine_message,
    meaningful_word_count,
    normalize_message_text,
    parse_channel_ids,
    snapshot_channel_ids,
    video_fingerprint,
)
from core.database import CommunityDatabase
from config import (
    CONTEST_CHANNEL_IDS,
    CONTEST_EXCLUDED_ROLE_IDS,
    CONTEST_MESSAGE_COOLDOWN_SECONDS,
    CONTEST_MIN_MESSAGE_WORDS,
    CONTEST_POINTS_MESSAGE,
    CONTEST_POINTS_REACTION,
    CONTEST_POINTS_TIKTOK,
)
from views.engagement_contest_view import (
    _parse_custom_contest_range,
    _report_timezone,
    get_today_range,
)

# Moderators pick the counted channels in the Mod Panel; tests use stand-ins.
TEST_CONTEST_CHANNEL_IDS = frozenset({101, 102, 103})

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
COOLDOWN_GAP = timedelta(seconds=CONTEST_MESSAGE_COOLDOWN_SECONDS + 1)


def message_event(
    user_id: int,
    text: str,
    *,
    offset_seconds: int = 0,
    key: str | None = None,
    name: str = "Member",
) -> StoredEvent:
    """A raw message event as the bot would have stored it."""
    return StoredEvent(
        user_id=user_id,
        user_name=name,
        event_type="message",
        occurred_at=NOW + timedelta(seconds=offset_seconds),
        event_key=key or f"contest_msg:{user_id}:{offset_seconds}",
        content_hash=content_fingerprint(text),
        word_count=meaningful_word_count(text),
    )


def tiktok_event(
    user_id: int, url: str, *, offset_seconds: int = 0, key: str | None = None
) -> StoredEvent:
    return StoredEvent(
        user_id=user_id,
        user_name="Member",
        event_type="tiktok",
        occurred_at=NOW + timedelta(seconds=offset_seconds),
        event_key=key or f"contest_msg:{user_id}:{offset_seconds}",
        content_hash=video_fingerprint([url]),
    )


def reaction_event(user_id: int, *, offset_seconds: int = 0) -> StoredEvent:
    return StoredEvent(
        user_id=user_id,
        user_name="Member",
        event_type="reaction",
        occurred_at=NOW + timedelta(seconds=offset_seconds),
        event_key=f"contest_reaction:{user_id}:{offset_seconds}",
    )


class TestPointValues(unittest.TestCase):
    """The agreed contest mechanics."""

    def test_configured_weighting(self):
        self.assertEqual(CONTEST_POINTS_MESSAGE, 2)
        self.assertEqual(CONTEST_POINTS_REACTION, 1)
        self.assertEqual(CONTEST_POINTS_TIKTOK, 10)

    def test_counted_channels(self):
        # SafeRest's contest channels are filled in before the first contest.
        self.assertIsInstance(CONTEST_CHANNEL_IDS, set)


class TestTikTokLinkDetection(unittest.TestCase):
    def test_standard_tiktok_urls(self):
        self.assertTrue(
            contains_tiktok_link(
                "Check this out https://www.tiktok.com/@user/video/1234567890123"
            )
        )
        self.assertTrue(contains_tiktok_link("https://tiktok.com/@brand/video/9876543210"))

    def test_short_and_mobile_urls(self):
        self.assertTrue(contains_tiktok_link("https://vt.tiktok.com/ZS2xYAbc/"))
        self.assertTrue(contains_tiktok_link("https://vm.tiktok.com/ZM8abc/"))
        self.assertTrue(contains_tiktok_link("http://m.tiktok.com/v/12345.html"))

    def test_urls_with_query_parameters(self):
        self.assertTrue(
            contains_tiktok_link(
                "My submission: https://www.tiktok.com/@saferest/video/12345"
                "?is_from_webapp=1&sender_device=pc"
            )
        )

    def test_non_tiktok_urls(self):
        self.assertFalse(contains_tiktok_link("https://youtube.com/watch?v=123"))
        self.assertFalse(contains_tiktok_link("https://instagram.com/p/123"))
        self.assertFalse(contains_tiktok_link("https://not-tiktok.com/video"))
        self.assertFalse(contains_tiktok_link("plain message without any links"))
        self.assertFalse(contains_tiktok_link(None))

    def test_embed_urls(self):
        mock_embed = MagicMock()
        mock_embed.url = "https://www.tiktok.com/@user/video/123"
        mock_embed.description = None
        self.assertTrue(contains_tiktok_link("", embeds=[mock_embed]))

        mock_embed_desc = MagicMock()
        mock_embed_desc.url = None
        mock_embed_desc.description = "Link here: https://vt.tiktok.com/ABC/"
        self.assertTrue(contains_tiktok_link(None, embeds=[mock_embed_desc]))

    def test_distinct_links_are_deduplicated(self):
        links = extract_tiktok_links(
            "https://www.tiktok.com/@a/video/1 and again "
            "https://www.tiktok.com/@a/video/1 plus https://vt.tiktok.com/ZZZ/"
        )
        self.assertEqual(len(links), 2)


class TestGenuineMessageRules(unittest.TestCase):
    def test_min_word_threshold_is_more_than_two(self):
        self.assertEqual(CONTEST_MIN_MESSAGE_WORDS, 3)

    def test_short_messages_are_not_genuine(self):
        self.assertFalse(is_genuine_message("ok"))
        self.assertFalse(is_genuine_message("nice one"))
        self.assertFalse(is_genuine_message("+1"))
        self.assertFalse(is_genuine_message(""))
        self.assertFalse(is_genuine_message(None))

    def test_three_real_words_qualify(self):
        self.assertTrue(is_genuine_message("this looks amazing"))
        self.assertTrue(
            is_genuine_message("I love the new linen collection so much")
        )

    def test_links_mentions_and_emoji_are_not_words(self):
        self.assertFalse(
            is_genuine_message("<@123456789> https://example.com/a/b <:party:99887766>")
        )
        self.assertEqual(meaningful_word_count("check https://example.com now"), 2)
        self.assertFalse(is_genuine_message("check https://example.com now"))

    def test_normalization_collapses_punctuation_and_case(self):
        self.assertEqual(
            normalize_message_text("OMG!!! This  is   GREAT???"),
            "omg this is great",
        )
        self.assertEqual(
            content_fingerprint("So beautiful!!!"),
            content_fingerprint("so   beautiful"),
        )


class TestScoringEngine(unittest.TestCase):
    def test_basic_point_arithmetic(self):
        events = [
            message_event(1, "this linen set is gorgeous", offset_seconds=0),
            message_event(1, "where can I order this one", offset_seconds=60),
            tiktok_event(1, "https://www.tiktok.com/@a/video/1", offset_seconds=120),
            reaction_event(1, offset_seconds=130),
            reaction_event(1, offset_seconds=140),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(len(standings), 1)
        # 2 messages (4) + 1 tiktok (10) + 2 reactions (2) = 16
        self.assertEqual(standings[0].total_points, 16)
        self.assertEqual(standings[0].messages, 2)
        self.assertEqual(standings[0].tiktoks, 1)
        self.assertEqual(standings[0].reactions, 2)
        self.assertEqual(stats.total_filtered, 0)

    def test_too_short_messages_score_nothing(self):
        events = [
            message_event(1, "ok", offset_seconds=0),
            message_event(1, "nice", offset_seconds=60),
            message_event(1, "great", offset_seconds=120),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(standings, [])
        self.assertEqual(stats.too_short, 3)

    def test_same_member_repeating_text_scores_once(self):
        events = [
            message_event(1, "good morning everyone here", offset_seconds=0),
            message_event(1, "good morning everyone here", offset_seconds=3600),
            message_event(1, "good morning everyone here", offset_seconds=7200),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(standings[0].messages, 1)
        self.assertEqual(standings[0].total_points, CONTEST_POINTS_MESSAGE)
        self.assertEqual(stats.repeated, 2)

    def test_same_member_may_repeat_after_the_window(self):
        events = [
            message_event(1, "good morning everyone here", offset_seconds=0),
            message_event(
                1, "good morning everyone here", offset_seconds=int(timedelta(hours=25).total_seconds())
            ),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(standings[0].messages, 2)
        self.assertEqual(stats.repeated, 0)

    def test_copy_pasted_text_only_scores_for_the_first_poster(self):
        events = [
            message_event(1, "this bedding collection is stunning", offset_seconds=0),
            message_event(2, "this bedding collection is stunning", offset_seconds=30),
            message_event(3, "this bedding collection is stunning", offset_seconds=60),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(len(standings), 1)
        self.assertEqual(standings[0].user_id, 1)
        self.assertEqual(stats.copy_pasted, 2)

    def test_copy_paste_block_expires(self):
        events = [
            message_event(1, "this bedding collection is stunning", offset_seconds=0),
            message_event(
                2,
                "this bedding collection is stunning",
                offset_seconds=int(timedelta(hours=2).total_seconds()),
            ),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(len(standings), 2)
        self.assertEqual(stats.copy_pasted, 0)

    def test_rapid_fire_bursts_are_rate_limited(self):
        events = [
            message_event(1, "first genuine thought here", offset_seconds=0),
            message_event(1, "second genuine thought here", offset_seconds=1),
            message_event(1, "third genuine thought here", offset_seconds=2),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(standings[0].messages, 1)
        self.assertEqual(stats.rate_limited, 2)

    def test_messages_outside_the_cooldown_all_score(self):
        gap = int(COOLDOWN_GAP.total_seconds())
        events = [
            message_event(1, "first genuine thought here", offset_seconds=0),
            message_event(1, "second genuine thought here", offset_seconds=gap),
            message_event(1, "third genuine thought here", offset_seconds=gap * 2),
        ]
        standings, stats = build_standings(events)
        self.assertEqual(standings[0].messages, 3)
        self.assertEqual(stats.rate_limited, 0)

    def test_same_clip_reposted_scores_once_per_member(self):
        url = "https://www.tiktok.com/@saferest/video/555"
        events = [
            tiktok_event(1, url, offset_seconds=0, key="a"),
            tiktok_event(1, url, offset_seconds=600, key="b"),
            tiktok_event(2, url, offset_seconds=900, key="c"),
        ]
        standings, stats = build_standings(events)
        by_user = {item.user_id: item for item in standings}
        self.assertEqual(by_user[1].tiktoks, 1)
        self.assertEqual(by_user[2].tiktoks, 1)
        self.assertEqual(stats.duplicate_videos, 1)

    def test_video_posts_are_exempt_from_the_word_rule(self):
        events = [tiktok_event(1, "https://vt.tiktok.com/ABC/", offset_seconds=0)]
        standings, _ = build_standings(events)
        self.assertEqual(standings[0].total_points, CONTEST_POINTS_TIKTOK)

    def test_ranking_order_and_zero_point_members_dropped(self):
        events = [
            # User 1: one video = 10 pts
            tiktok_event(1, "https://www.tiktok.com/@a/video/1", offset_seconds=0),
            # User 2: three genuine messages = 6 pts
            message_event(2, "the napkins look so soft", offset_seconds=0),
            message_event(2, "I ordered mine last week", offset_seconds=60),
            message_event(2, "shipping was really quick too", offset_seconds=120),
            # User 3: only short messages, should not appear at all
            message_event(3, "nice", offset_seconds=0),
        ]
        standings, _ = build_standings(events)
        self.assertEqual([item.user_id for item in standings], [1, 2])
        self.assertEqual(standings[0].total_points, 10)
        self.assertEqual(standings[1].total_points, 6)

    def test_scoring_is_deterministic_regardless_of_input_order(self):
        events = [
            message_event(1, "this bedding collection is stunning", offset_seconds=0),
            message_event(2, "this bedding collection is stunning", offset_seconds=30),
        ]
        forward, _ = build_standings(events)
        backward, _ = build_standings(list(reversed(events)))
        self.assertEqual(
            [item.as_dict() for item in forward], [item.as_dict() for item in backward]
        )


class TestContestChannelFilter(unittest.TestCase):
    def test_contest_channels(self):
        for channel_id in TEST_CONTEST_CHANNEL_IDS:
            mock_channel = MagicMock()
            mock_channel.id = channel_id
            self.assertTrue(is_contest_channel(mock_channel, TEST_CONTEST_CHANNEL_IDS))

    def test_thread_in_contest_channel(self):
        parent_id = next(iter(TEST_CONTEST_CHANNEL_IDS))
        mock_thread = MagicMock()
        mock_thread.id = 999999999999
        mock_thread.parent_id = parent_id
        self.assertTrue(is_contest_channel(mock_thread, TEST_CONTEST_CHANNEL_IDS))

    def test_non_contest_channel(self):
        mock_channel = MagicMock()
        mock_channel.id = 111111111111
        mock_channel.parent_id = None
        self.assertFalse(is_contest_channel(mock_channel, TEST_CONTEST_CHANNEL_IDS))
        self.assertFalse(is_contest_channel(None, TEST_CONTEST_CHANNEL_IDS))

    def test_nothing_counts_without_chosen_channels(self):
        mock_channel = MagicMock()
        mock_channel.id = 101
        mock_channel.parent_id = None
        self.assertFalse(is_contest_channel(mock_channel, frozenset()))


class TestContestSettings(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = CommunityDatabase(Path(self.temp_dir.name) / "community.sqlite3")
        await self.db.initialize()
        self.cog = EngagementContestCog.__new__(EngagementContestCog)
        self.cog.db = self.db
        self.cog._settings = {}

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    def test_parse_channel_ids(self):
        self.assertIsNone(parse_channel_ids(None))
        self.assertEqual(parse_channel_ids(""), frozenset())
        self.assertEqual(parse_channel_ids("3,1, 2"), frozenset({1, 2, 3}))

    async def test_nothing_chosen_by_default(self):
        settings = await self.cog.settings(1)
        self.assertEqual(settings.channel_ids, frozenset())
        self.assertIsNone(settings.report_channel_id)

    async def test_chosen_channels_are_saved_per_server(self):
        await self.cog.settings(1)  # warm the cache, which a change must clear
        await self.cog.set_channels(1, [30, 10, 20, 10])
        await self.cog.set_report_channel(1, 99)
        settings = await self.cog.settings(1)
        self.assertEqual(settings.channel_ids, frozenset({10, 20, 30}))
        self.assertEqual(settings.report_channel_id, 99)
        self.assertEqual(await self.db.get_setting(1, CHANNELS_SETTING), "10,20,30")
        self.assertEqual(await self.db.get_setting(1, REPORT_CHANNEL_SETTING), "99")
        self.assertEqual((await self.cog.settings(2)).channel_ids, frozenset())

    async def test_clearing_stops_counting(self):
        await self.cog.set_channels(1, [10])
        await self.cog.set_channels(1, [])
        await self.cog.set_report_channel(1, None)
        settings = await self.cog.settings(1)
        self.assertEqual(settings.channel_ids, frozenset())
        self.assertIsNone(settings.report_channel_id)

    def test_snapshot_remembers_counted_channels(self):
        self.assertIsNone(snapshot_channel_ids({"standings": []}))
        self.assertEqual(snapshot_channel_ids({"channel_ids": [5, 6]}), [5, 6])


class TestExcludedMemberFilter(unittest.TestCase):
    def test_bot_excluded(self):
        mock_member = MagicMock()
        mock_member.bot = True
        mock_member.roles = []
        self.assertTrue(is_excluded_member(mock_member))

    def test_excluded_roles(self):
        for role_id in CONTEST_EXCLUDED_ROLE_IDS:
            mock_member = MagicMock()
            mock_member.bot = False
            mock_role = MagicMock()
            mock_role.id = role_id
            mock_member.roles = [mock_role]
            self.assertTrue(is_excluded_member(mock_member))

    def test_regular_qualifying_member(self):
        mock_member = MagicMock()
        mock_member.bot = False
        regular_role = MagicMock()
        regular_role.id = 123456789
        mock_member.roles = [regular_role]
        self.assertFalse(is_excluded_member(mock_member))


class TestContestStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_contest.sqlite3"
        self.store = ContestStore(self.db_path)
        await self.store.initialize()
        self.guild_id = 1000
        self.channel_id = next(iter(TEST_CONTEST_CHANNEL_IDS))

    async def asyncTearDown(self):
        await self.store.close()
        self.temp_dir.cleanup()

    def _event(self, user_id, text, event_type, offset, key):
        return ContestEvent(
            guild_id=self.guild_id,
            user_id=user_id,
            user_name="Member",
            channel_id=self.channel_id,
            event_type=event_type,
            occurred_at=NOW + timedelta(seconds=offset),
            event_key=key,
            points=0,
            content_hash=content_fingerprint(text),
            word_count=meaningful_word_count(text),
        )

    async def test_round_trip_and_idempotence(self):
        events = [
            self._event(101, "this linen feels incredible", "message", 0, "m1"),
            self._event(101, "ordering another set today", "message", 60, "m2"),
            self._event(101, "", "reaction", 90, "r1"),
        ]
        self.assertEqual(await self.store.record_events(events), 3)
        self.assertEqual(await self.store.record_events([events[0]]), 0)

        standings = await self.store.aggregate(
            self.guild_id, NOW - timedelta(hours=1), NOW + timedelta(hours=1)
        )
        self.assertEqual(len(standings), 1)
        # 2 messages (4 pts) + 1 reaction (1 pt)
        self.assertEqual(standings[0].total_points, 5)

    async def test_window_excludes_events_outside_the_contest(self):
        await self.store.record_events(
            [
                self._event(101, "before the contest started", "message", -7200, "old"),
                self._event(101, "after the contest started", "message", 60, "new"),
            ]
        )
        standings = await self.store.aggregate(
            self.guild_id, NOW, NOW + timedelta(hours=1)
        )
        self.assertEqual(standings[0].messages, 1)
        self.assertEqual(standings[0].total_points, CONTEST_POINTS_MESSAGE)

    async def test_session_lifecycle(self):
        self.assertIsNone(await self.store.get_active_session(self.guild_id))

        first = await self.store.start_session(
            guild_id=self.guild_id,
            name="September Contest",
            started_at=NOW,
            started_by_id=7,
            started_by_name="Aljay",
        )
        self.assertTrue(first.is_active)
        active = await self.store.get_active_session(self.guild_id)
        self.assertEqual(active.id, first.id)

        # Starting another contest closes the one still running.
        second = await self.store.start_session(
            guild_id=self.guild_id,
            name="October Contest",
            started_at=NOW + timedelta(days=30),
            started_by_id=7,
            started_by_name="Aljay",
        )
        self.assertNotEqual(second.id, first.id)
        active = await self.store.get_active_session(self.guild_id)
        self.assertEqual(active.id, second.id)
        self.assertFalse((await self.store.get_session(first.id)).is_active)

        ended = await self.store.end_session(
            second.id,
            ended_at=NOW + timedelta(days=60),
            ended_by_name="Aljay",
            snapshot={"standings": [], "channels_inspected": 4},
        )
        self.assertFalse(ended.is_active)
        self.assertEqual(ended.snapshot["channels_inspected"], 4)
        self.assertIsNone(await self.store.get_active_session(self.guild_id))

        history = await self.store.list_sessions(self.guild_id)
        self.assertEqual([item.id for item in history], [second.id, first.id])

    async def test_reset_wipes_events_and_sessions(self):
        await self.store.record_events(
            [self._event(101, "this linen feels incredible", "message", 0, "m1")]
        )
        await self.store.start_session(
            guild_id=self.guild_id,
            name="Doomed Contest",
            started_at=NOW,
            started_by_id=7,
            started_by_name="Aljay",
        )
        events, sessions = await self.store.reset_guild(self.guild_id)
        self.assertEqual((events, sessions), (1, 1))
        self.assertIsNone(await self.store.get_active_session(self.guild_id))
        self.assertEqual(await self.store.list_sessions(self.guild_id), [])
        self.assertEqual(
            await self.store.aggregate(
                self.guild_id, NOW - timedelta(days=1), NOW + timedelta(days=1)
            ),
            [],
        )


class TestContestDateRanges(unittest.TestCase):
    def test_today_range(self):
        start_utc, end_utc, label = get_today_range()
        tz = _report_timezone()
        start_local = start_utc.astimezone(tz)
        self.assertEqual(start_local.hour, 0)
        self.assertEqual(start_local.minute, 0)
        self.assertEqual(start_local.second, 0)
        self.assertTrue(end_utc > start_utc)
        self.assertIn("Starting Today", label)

    def test_custom_range_valid(self):
        start_utc, end_utc = _parse_custom_contest_range("2026-09-01", "2026-09-05")
        self.assertTrue(end_utc > start_utc)

        start_time_utc, end_time_utc = _parse_custom_contest_range(
            "2026-09-01 08:00", "2026-09-01 20:00"
        )
        self.assertEqual((end_time_utc - start_time_utc).total_seconds(), 12 * 3600)

    def test_custom_range_invalid(self):
        with self.assertRaises(ValueError):
            _parse_custom_contest_range("2026-09-05", "2026-09-01")

        with self.assertRaises(ValueError):
            _parse_custom_contest_range("not-a-date", "2026-09-01")


if __name__ == "__main__":
    unittest.main()
