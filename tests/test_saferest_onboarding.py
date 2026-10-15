"""
Unit tests for SafeRest Onboarding (private per-member onboarding channels).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import asyncio
import unittest

import discord

import cogs.saferest_onboarding as saferest
from cogs.saferest_onboarding import (
    CATEGORY_LIMIT,
    SELF_SERVE_BATCH,
    SETTING_KEYS,
    TICKET_INTRO,
    TOPIC_TEMPLATE,
    SafeRestOnboardingCog,
    SafeRestSettings,
    TaggedGroup,
    can_close,
    channel_name,
    is_staff_role,
    owner_from_topic,
    ticket_overwrites,
)
from cogs.creator_onboarding import WELCOME_MESSAGE
from cogs.onboarding import DayGroup
from core.database import CommunityDatabase
from views.saferest_onboarding_view import (
    SafeRestComposerView,
    SafeRestDraft,
    SafeRestOnboardingHubView,
    SelfOnboardingView,
    SelfPostView,
    build_composer_embed,
    build_self_post_embed,
    build_self_start_embed,
)
from views.creator_onboarding_view import OnboardingTicketView

GUILD_ID = 1
ROLE_ID = 700
CATEGORY_ID = 800


class FakeRole:
    def __init__(self, role_id, position, permissions=None, *, default=False, managed=False):
        self.id = role_id
        self.position = position
        self.permissions = permissions or discord.Permissions.none()
        self.managed = managed
        self._default = default
        self.mention = f"<@&{role_id}>"

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position

    def __hash__(self):
        return self.id


class FakeChannel:
    def __init__(self, channel_id, name="", topic=None, category=None, overwrites=None):
        self.id = channel_id
        self.name = name
        self.topic = topic
        self.category = category
        self.overwrites = overwrites or {}
        self.mention = f"<#{channel_id}>"
        self.sent = []
        self.guild = SimpleNamespace(id=GUILD_ID)

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(jump_url=f"https://discord.test/{self.id}")


class FakeCategory(discord.CategoryChannel):
    """A real CategoryChannel subclass so isinstance checks pass."""

    channels = None  # shadows the read-only property

    def __init__(self, category_id=CATEGORY_ID, count=0, manage=True):  # noqa: D107
        self.id = category_id
        self.name = "Onboarding"
        self.channels = [object()] * count
        self._manage = manage

    def permissions_for(self, member):
        return SimpleNamespace(manage_channels=self._manage)


class FakeTextChannel(discord.TextChannel):
    """A real TextChannel subclass so isinstance checks pass."""

    category = overwrites = mention = None  # shadow the read-only properties

    def __init__(self, channel_id, name="", topic=None, category=None, overwrites=None):  # noqa: D107
        self.id = channel_id
        self.name = name
        self.topic = topic
        self.category = category
        self.overwrites = overwrites or {}
        self.mention = f"<#{channel_id}>"
        self.guild = SimpleNamespace(id=GUILD_ID)
        self.sent = []
        self.deleted = None

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(jump_url=f"https://discord.test/{self.id}")

    async def delete(self, *, reason=None):
        self.deleted = reason


class FakeMember:
    def __init__(self, guild, member_id, name, *, days_ago=0, roles=None):
        self.id = member_id
        self.name = name
        self.display_name = name.title()
        self.mention = f"<@{member_id}>"
        self.bot = False
        self.guild = guild
        self.roles = list(roles or [])
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)
        self.added = []
        self.fail_add = False

    async def add_roles(self, role, reason=None):
        if self.fail_add:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
        self.added.append(role)
        self.roles.append(role)

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self, *, category=None, bot_position=10, manage_roles=True):
        self.id = GUILD_ID
        self.chunked = True
        self.default_role = FakeRole(GUILD_ID, 0, default=True)
        self.access_role = FakeRole(ROLE_ID, 5)
        self.mod_role = FakeRole(600, 4, discord.Permissions(manage_messages=True))
        self.plain_role = FakeRole(601, 3)
        self.bot_role = FakeRole(602, bot_position, discord.Permissions(administrator=True), managed=True)
        self.roles = [self.default_role, self.access_role, self.mod_role, self.plain_role, self.bot_role]
        self.me = discord.Object(999)
        self.me.top_role = self.bot_role
        self.me.guild_permissions = SimpleNamespace(manage_roles=manage_roles)
        self.category_channel = category if category is not None else FakeCategory()
        self.channels = {CATEGORY_ID: self.category_channel}
        self.members = []
        self.created = []

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    async def create_text_channel(self, name, *, category, overwrites, topic, reason):
        channel = FakeTextChannel(
            5000 + len(self.created), name, topic=topic, category=category, overwrites=overwrites
        )
        self.created.append(channel)
        self.channels[channel.id] = channel
        return channel


def view_factory():
    return "VIEW"


class HelperTests(unittest.TestCase):
    def test_channel_names(self):
        self.assertEqual(channel_name(SimpleNamespace(name="lemon", id=1)), "lemon-onboarding")
        self.assertEqual(channel_name(SimpleNamespace(name="Lemon.PC", id=1)), "lemon-pc-onboarding")
        self.assertEqual(channel_name(SimpleNamespace(name="__", id=7)), "member-7-onboarding")

    def test_owner_from_topic(self):
        topic = TOPIC_TEMPLATE.format(member="lemon", member_id=123456789012345678)
        self.assertEqual(owner_from_topic(topic), 123456789012345678)
        self.assertIsNone(owner_from_topic("General chat"))
        self.assertIsNone(owner_from_topic(None))

    def test_owner_or_moderators_can_close(self):
        def channel(permissions):
            return SimpleNamespace(permissions_for=lambda member: permissions)

        member = SimpleNamespace(id=42)
        nobody = discord.Permissions.none()
        self.assertTrue(can_close(member, channel(nobody), 42))
        self.assertFalse(can_close(SimpleNamespace(id=7), channel(nobody), 42))
        mod = channel(discord.Permissions(manage_messages=True))
        self.assertTrue(can_close(SimpleNamespace(id=7), mod, 42))
        self.assertFalse(can_close(member, mod, None))  # not an onboarding channel

    def test_only_member_staff_and_bot_can_see_the_channel(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        overwrites = ticket_overwrites(guild, member)
        self.assertIs(overwrites[guild.default_role].view_channel, False)
        self.assertIs(overwrites[member].view_channel, True)
        self.assertIs(overwrites[member].send_messages, True)
        self.assertIs(overwrites[guild.mod_role].view_channel, True)
        self.assertIs(overwrites[guild.me].view_channel, True)
        self.assertNotIn(guild.plain_role, overwrites)
        self.assertNotIn(guild.access_role, overwrites)
        self.assertNotIn(guild.bot_role, overwrites)  # managed roles are skipped

    def test_admin_and_team_roles_count_as_staff(self):
        from config import ADMIN_ROLE_ID, TEAM_ROLE_ID, VERIFIED_ROLE_ID

        self.assertTrue(is_staff_role(FakeRole(ADMIN_ROLE_ID, 9)))
        self.assertTrue(is_staff_role(FakeRole(TEAM_ROLE_ID, 8)))
        self.assertFalse(is_staff_role(FakeRole(VERIFIED_ROLE_ID, 3)))
        nobody = SimpleNamespace(permissions_for=lambda member: discord.Permissions.none())
        team_member = SimpleNamespace(id=7, roles=[FakeRole(TEAM_ROLE_ID, 8)])
        self.assertTrue(can_close(team_member, nobody, 42))


class SafeRestTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.db = CommunityDatabase(Path(self.tmp.name) / "community.sqlite3")
        await self.db.initialize()
        self.cog = SafeRestOnboardingCog.__new__(SafeRestOnboardingCog)
        self.cog.bot = None
        self.cog.db = self.db
        self.cog._starting = set()
        self._pace = saferest.CREATE_PACE_SECONDS
        saferest.CREATE_PACE_SECONDS = 0
        self._defaults = dict(saferest.ENV_DEFAULTS)
        saferest.ENV_DEFAULTS.update(category=CATEGORY_ID, role=ROLE_ID)

    async def asyncTearDown(self):
        saferest.CREATE_PACE_SECONDS = self._pace
        saferest.ENV_DEFAULTS.clear()
        saferest.ENV_DEFAULTS.update(self._defaults)
        await self.db.close()
        self.tmp.cleanup()

    async def deliver(self, guild, members, batch_id):
        return await self.cog.deliver(
            guild=guild,
            members=members,
            moderator=SimpleNamespace(id=9),
            batch_id=batch_id,
            view_factory=view_factory,
        )


class SettingsTests(SafeRestTestCase):
    async def test_defaults_then_saved_values(self):
        settings = await self.cog.settings(GUILD_ID)
        self.assertEqual((settings.category_id, settings.role_id), (CATEGORY_ID, ROLE_ID))
        self.assertEqual(
            settings.sources, {"category": "env", "role": "env", "verified": "env"}
        )
        await self.cog.set_value(GUILD_ID, "role", 701)
        settings = await self.cog.settings(GUILD_ID)
        self.assertEqual((settings.role_id, settings.sources["role"]), (701, "saved"))
        self.assertEqual(await self.db.get_setting(GUILD_ID, SETTING_KEYS["role"]), "701")

    async def test_verified_role_can_be_chosen(self):
        from config import VERIFIED_ROLE_ID

        settings = await self.cog.settings(GUILD_ID)
        self.assertEqual(settings.verified_role_id, VERIFIED_ROLE_ID)
        await self.cog.set_value(GUILD_ID, "verified", 702)
        settings = await self.cog.settings(GUILD_ID)
        self.assertEqual((settings.verified_role_id, settings.sources["verified"]), (702, "saved"))
        self.assertEqual(settings.role_id, ROLE_ID)  # the access role is untouched

    async def test_setup_problems(self):
        settings = await self.cog.settings(GUILD_ID)
        self.assertEqual(self.cog.setup_problems(FakeGuild(), settings), [])
        broken = FakeGuild(bot_position=2, manage_roles=False)
        broken.channels.clear()
        problems = " ".join(self.cog.setup_problems(broken, settings))
        self.assertIn("category", problems)
        self.assertIn("Manage Roles", problems)
        self.assertIn("above", problems)
        no_manage = FakeGuild(category=FakeCategory(manage=False))
        self.assertIn("Manage Channels", " ".join(self.cog.setup_problems(no_manage, settings)))


class TicketTests(SafeRestTestCase):
    async def test_open_creates_private_channel_and_tags_member(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        result = await self.deliver(guild, [member], batch_id=1)
        self.assertEqual(len(result.created), 1)
        self.assertEqual(result.member_ids, [42])
        channel = guild.created[0]
        self.assertEqual(channel.name, "lemon-onboarding")
        self.assertIs(channel.category, guild.category_channel)
        self.assertEqual(owner_from_topic(channel.topic), 42)
        sent = channel.sent[0]
        self.assertTrue(sent["content"].startswith(TICKET_INTRO.format(mention="<@42>")))
        self.assertTrue(sent["content"].endswith(WELCOME_MESSAGE))
        self.assertIn("unlock all of the server's channels", sent["content"])
        self.assertIn("Got a question", sent["content"])
        self.assertEqual(sent["view"], "VIEW")
        self.assertEqual(await self.cog.owner_of(channel), 42)

    async def test_owner_lookup_falls_back_to_the_database(self):
        guild = FakeGuild()
        await self.deliver(guild, [FakeMember(guild, 42, "lemon")], batch_id=1)
        channel = guild.created[0]
        channel.topic = "renamed by a moderator"
        self.assertEqual(await self.cog.owner_of(channel), 42)
        self.assertIsNone(await self.cog.owner_of(FakeChannel(1234)))

    async def test_clear_last_group_and_retag_reuses_channel(self):
        guild = FakeGuild()
        first = [FakeMember(guild, 1, "ann"), FakeMember(guild, 2, "bob")]
        second = [FakeMember(guild, 3, "cat")]
        await self.deliver(guild, first, batch_id=100)
        await asyncio.sleep(0.01)
        await self.deliver(guild, second, batch_id=200)
        self.assertEqual(await self.cog.tagged_ids(GUILD_ID), {1, 2, 3})

        group = await self.cog.last_group(GUILD_ID)
        self.assertEqual((group.batch_id, group.count), (200, 1))
        self.assertEqual(await self.cog.clear_last_group(GUILD_ID), 1)
        self.assertEqual(await self.cog.tagged_ids(GUILD_ID), {1, 2})
        self.assertEqual((await self.cog.last_group(GUILD_ID)).batch_id, 100)

        again = await self.deliver(guild, second, batch_id=300)
        self.assertEqual((len(again.created), len(again.reused)), (0, 1))
        self.assertEqual(len(guild.created), 3)
        self.assertEqual(len(guild.created[2].sent), 2)  # re-tagged in the same channel

    async def test_test_run_does_not_tag(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        channel, created = await self.cog.open_ticket(
            member, guild.category_channel, "VIEW", batch_id=None, tagged_by=None
        )
        self.assertTrue(created)
        self.assertEqual(await self.cog.tagged_ids(GUILD_ID), set())
        self.assertIsNone(await self.cog.last_group(GUILD_ID))
        self.assertEqual(await self.cog.owner_of(channel), 42)

    async def test_full_category_is_reported(self):
        guild = FakeGuild(category=FakeCategory(count=CATEGORY_LIMIT))
        result = await self.deliver(guild, [FakeMember(guild, 1, "ann")], batch_id=1)
        self.assertEqual(result.opened, 0)
        self.assertIn("category is full", result.failed[0][1])
        self.assertEqual(await self.cog.tagged_ids(GUILD_ID), set())

    async def test_pending_skips_tagged_and_members_with_access(self):
        guild = FakeGuild()
        fresh = FakeMember(guild, 1, "ann")
        has_access = FakeMember(guild, 2, "bob", roles=[guild.access_role])
        tagged = FakeMember(guild, 3, "cat")
        old = FakeMember(guild, 4, "dan", days_ago=400)
        guild.members = [fresh, has_access, tagged, old]
        await self.deliver(guild, [tagged], batch_id=1)
        self.assertEqual([m.id for m in await self.cog.pending_members(guild)], [1])


class CompletionTests(SafeRestTestCase):
    async def test_grants_role_and_counts_completion(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        guild.members = [member]
        await self.deliver(guild, [member], batch_id=1)
        self.assertTrue(await self.cog.complete(member))
        self.assertEqual(member.added, [guild.access_role])
        self.assertTrue(await self.cog.complete(member))  # already has it: no second add
        self.assertEqual(len(member.added), 1)
        stats = await self.cog.stats(guild)
        self.assertEqual((stats["channels"], stats["completed"], stats["pending"]), (1, 1, 0))

    async def test_role_failure_returns_false(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        member.fail_add = True
        self.assertFalse(await self.cog.complete(member))
        await self.cog.set_value(GUILD_ID, "role", 12345)  # a role that does not exist
        member.fail_add = False
        self.assertFalse(await self.cog.complete(member))


class SelfStartTests(SafeRestTestCase):
    async def test_member_opens_their_own_channel(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        guild.members = [member]
        result = await self.cog.self_start(member, "VIEW")
        self.assertEqual(result.status, "opened")
        self.assertIs(result.channel, guild.created[0])
        self.assertEqual(result.channel.name, "lemon-onboarding")
        self.assertEqual(owner_from_topic(result.channel.topic), 42)
        self.assertEqual(result.channel.sent[0]["view"], "VIEW")
        # Counted as tagged, so scans skip them, and shown in the stats.
        self.assertEqual(await self.cog.tagged_ids(GUILD_ID), {42})
        self.assertEqual(await self.cog.pending_members(guild), [])
        self.assertEqual((await self.cog.stats(guild))["self_started"], 1)

    async def test_second_press_points_back_to_the_same_channel(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        first = await self.cog.self_start(member, "VIEW")
        again = await self.cog.self_start(member, "VIEW")
        self.assertEqual(again.status, "existing")
        self.assertIs(again.channel, first.channel)
        self.assertEqual(len(guild.created), 1)
        self.assertEqual(len(first.channel.sent), 1)  # not tagged a second time

    async def test_double_click_opens_one_channel(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        results = await asyncio.gather(
            self.cog.self_start(member, "VIEW"), self.cog.self_start(member, "VIEW")
        )
        self.assertEqual(sorted(r.status for r in results), ["busy", "opened"])
        self.assertEqual(len(guild.created), 1)

    async def test_closed_channel_can_be_started_again(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        first = await self.cog.self_start(member, "VIEW")
        await self.cog.close_ticket(first.channel, closed_by=member)
        again = await self.cog.self_start(member, "VIEW")
        self.assertEqual(again.status, "opened")
        self.assertEqual(len(guild.created), 2)

    async def test_members_with_access_are_told_they_are_set(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon", roles=[guild.access_role])
        result = await self.cog.self_start(member, "VIEW")
        self.assertEqual((result.status, result.channel), ("has_access", None))
        self.assertEqual(guild.created, [])

    async def test_missing_category_or_full_category(self):
        broken = FakeGuild()
        broken.channels.clear()
        self.assertEqual(
            (await self.cog.self_start(FakeMember(broken, 1, "ann"), "VIEW")).status, "unavailable"
        )
        no_manage = FakeGuild(category=FakeCategory(manage=False))
        self.assertEqual(
            (await self.cog.self_start(FakeMember(no_manage, 1, "ann"), "VIEW")).status, "unavailable"
        )
        full = FakeGuild(category=FakeCategory(count=CATEGORY_LIMIT))
        self.assertEqual(
            (await self.cog.self_start(FakeMember(full, 1, "ann"), "VIEW")).status, "full"
        )
        self.assertEqual(await self.cog.tagged_ids(GUILD_ID), set())

    async def test_self_starters_are_never_the_last_tagged_group(self):
        guild = FakeGuild()
        await self.deliver(guild, [FakeMember(guild, 1, "ann")], batch_id=100)
        await asyncio.sleep(0.01)
        await self.cog.self_start(FakeMember(guild, 2, "bob"), "VIEW")
        group = await self.cog.last_group(GUILD_ID)
        self.assertEqual((group.batch_id, group.count), (100, 1))
        self.assertEqual(await self.cog.clear_last_group(GUILD_ID), 1)
        self.assertEqual(await self.cog.tagged_ids(GUILD_ID), {2})
        self.assertIsNone(await self.cog.last_group(GUILD_ID))

    async def test_moderator_tag_moves_self_starter_into_their_group(self):
        guild = FakeGuild()
        member = FakeMember(guild, 2, "bob")
        await self.cog.self_start(member, "VIEW")
        result = await self.deliver(guild, [member], batch_id=100)
        self.assertEqual(len(result.reused), 1)
        self.assertEqual((await self.cog.last_group(GUILD_ID)).batch_id, 100)
        self.assertEqual((await self.cog.stats(guild))["self_started"], 0)


class CloseTests(SafeRestTestCase):
    async def test_close_deletes_and_next_tag_opens_a_fresh_channel(self):
        guild = FakeGuild()
        member = FakeMember(guild, 42, "lemon")
        await self.deliver(guild, [member], batch_id=1)
        channel = guild.created[0]
        await self.cog.close_ticket(channel, closed_by=member)
        self.assertIn("closed by lemon", channel.deleted)
        self.assertEqual((await self.cog.stats(guild))["channels"], 0)
        self.assertEqual(await self.cog.owner_of(channel), 42)  # topic still names them
        await self.cog.clear_last_group(GUILD_ID)
        await self.deliver(guild, [member], batch_id=2)
        self.assertEqual(len(guild.created), 2)

    async def test_channel_deleted_by_hand_is_forgotten(self):
        guild = FakeGuild()
        await self.deliver(guild, [FakeMember(guild, 42, "lemon")], batch_id=1)
        await self.cog.on_guild_channel_delete(guild.created[0])
        self.assertEqual((await self.cog.stats(guild))["channels"], 0)


class ComposerTests(unittest.TestCase):
    def make_draft(self, *, groups=True, problems=(), last=None):
        members = [SimpleNamespace(id=1, display_name="Ann", joined_at=datetime.now(timezone.utc))]
        return SafeRestDraft(
            user_id=7,
            groups=[DayGroup("d0", "Joined today", "", members)] if groups else [],
            settings=SafeRestSettings(CATEGORY_ID, ROLE_ID, {"category": "env", "role": "env"}),
            last_group=last,
            problems=list(problems),
        )

    def test_ready_to_send(self):
        async def check():
            view = SafeRestComposerView(self.make_draft(last=TaggedGroup(5, 3, 1_700_000_000)))
            self.assertEqual(view.send.label, "Open 1 onboarding channel")
            self.assertFalse(view.send.disabled)
            self.assertFalse(view.clear_and_rescan.disabled)

        asyncio.run(check())

    def test_blocked_by_setup_problems_and_nothing_to_clear(self):
        async def check():
            view = SafeRestComposerView(self.make_draft(problems=["Manage Roles"]))
            self.assertTrue(view.send.disabled)
            self.assertTrue(view.clear_and_rescan.disabled)

        asyncio.run(check())

    def test_everyone_tagged_still_offers_clear_and_rescan(self):
        async def check():
            draft = self.make_draft(groups=False, last=TaggedGroup(5, 3, 1_700_000_000))
            view = SafeRestComposerView(draft)
            self.assertTrue(view.send.disabled)
            self.assertFalse(view.clear_and_rescan.disabled)
            self.assertFalse(any(isinstance(item, discord.ui.Select) for item in view.children))
            guild = SimpleNamespace(get_channel=lambda _: SimpleNamespace(name="Onboarding"))
            embed = build_composer_embed(draft, guild)
            self.assertIn("**3** members", embed.description)
            self.assertIn("<@&700>", embed.description)

        asyncio.run(check())

    def test_ticket_view_has_answers_and_close(self):
        async def check():
            view = OnboardingTicketView()
            self.assertTrue(view.is_persistent())
            self.assertEqual(
                {child.custom_id for child in view.children},
                {
                    "saferest_creator_onboarding:yes",
                    "saferest_creator_onboarding:no",
                    "saferest_onboarding:close",
                },
            )

        asyncio.run(check())

    def test_hub_is_persistent(self):
        async def check():
            hub = SafeRestOnboardingHubView()
            self.assertTrue(hub.is_persistent())
            self.assertEqual(
                {child.custom_id for child in hub.children},
                {
                    "saferest_onboarding_hub:scan",
                    "saferest_onboarding_hub:single_member",
                    "saferest_onboarding_hub:test",
                    "saferest_onboarding_hub:test_member",
                    "saferest_onboarding_hub:self_start_post",
                    "saferest_onboarding_hub:checkpoint",
                    "saferest_onboarding_hub:settings",
                    "saferest_onboarding_hub:stats",
                },
            )

        asyncio.run(check())

    def test_self_start_button_is_persistent(self):
        async def check():
            view = SelfOnboardingView()
            self.assertTrue(view.is_persistent())
            self.assertEqual(
                [child.custom_id for child in view.children], ["saferest_onboarding:self_start"]
            )
            self.assertIn("Start my onboarding", build_self_start_embed().description)

        asyncio.run(check())

    def test_self_post_view_needs_a_channel_and_working_setup(self):
        async def check():
            self.assertTrue(SelfPostView(None).post.disabled)
            self.assertFalse(SelfPostView(123).post.disabled)
            self.assertTrue(SelfPostView(123, problems=["no category"]).post.disabled)
            embed = build_self_post_embed(123, [("<#123>", "https://x")], problems=["no category"])
            self.assertIn("<#123>", embed.description)
            self.assertIn("no category", embed.description)
            self.assertIn("Posted this session", embed.description)

        asyncio.run(check())

    def test_single_member_selection_in_draft(self):
        member1 = SimpleNamespace(id=1, display_name="Ann", joined_at=datetime.now(timezone.utc))
        member2 = SimpleNamespace(id=2, display_name="Bob", joined_at=datetime.now(timezone.utc))
        draft = SafeRestDraft(
            user_id=7,
            groups=[DayGroup("d0", "Joined today", "", [member1, member2])],
            settings=SafeRestSettings(CATEGORY_ID, ROLE_ID, {"category": "env", "role": "env"}),
            last_group=None,
            problems=[],
        )
        self.assertEqual(len(draft.selected_members), 2)
        draft.select_single_member(2)
        self.assertEqual(len(draft.selected_members), 1)
        self.assertEqual(draft.selected_members[0].id, 2)

        view = SafeRestComposerView(draft)
        self.assertEqual(view.send.label, "Open onboarding for Bob")

        # Switching back to day selection clears single member mode
        draft.apply_selection({"all"})
        self.assertIsNone(draft.selected_member_id)
        self.assertEqual(len(draft.selected_members), 2)

    def test_test_member_select_view(self):
        from views.saferest_onboarding_view import SafeRestTestMemberSelectView

        view = SafeRestTestMemberSelectView()
        self.assertEqual(len(view.children), 2)
        self.assertTrue(any(isinstance(c, discord.ui.UserSelect) for c in view.children))
        self.assertTrue(any(isinstance(c, discord.ui.Button) for c in view.children))


if __name__ == "__main__":
    unittest.main()

