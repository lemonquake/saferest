"""
Auto onboarding: a member who joins gets the onboarding role, loses sight of
the hidden channel and gets a private channel; finishing undoes both.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

import cogs.saferest_onboarding as saferest
from tests.test_saferest_onboarding import (
    FakeGuild,
    FakeMember,
    FakeRole,
    SafeRestTestCase,
)
from views.mod_panel_view import ModPanelView
from views.saferest_onboarding_view import AutoOnboardingView, open_auto_onboarding_toggle


class FakeHiddenChannel:
    def __init__(self, manage=True):
        self.id = saferest.AUTO_HIDDEN_CHANNEL_ID
        self.mention = f"<#{self.id}>"
        self.member_overwrites = {}
        self._manage = manage

    def permissions_for(self, member):
        return SimpleNamespace(manage_roles=self._manage)

    def overwrites_for(self, member):
        return self.member_overwrites.get(member.id, discord.PermissionOverwrite())

    async def set_permissions(self, member, *, overwrite=discord.utils.MISSING, reason=None, **perms):
        if overwrite is None:
            self.member_overwrites.pop(member.id, None)
        else:
            self.member_overwrites[member.id] = discord.PermissionOverwrite(**perms)


class RemovableMember(FakeMember):
    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


def auto_guild():
    guild = FakeGuild()
    guild.auto_role = FakeRole(saferest.AUTO_ONBOARDING_ROLE_ID, 3)
    guild.roles.append(guild.auto_role)
    guild.hidden = FakeHiddenChannel()
    guild.channels[guild.hidden.id] = guild.hidden
    return guild


class AutoOnboardingTests(SafeRestTestCase):
    async def test_switch_is_off_until_turned_on(self):
        self.assertFalse(await self.cog.auto_enabled(1))
        await self.cog.set_auto_enabled(1, True)
        self.assertTrue(await self.cog.auto_enabled(1))
        await self.cog.set_auto_enabled(1, False)
        self.assertFalse(await self.cog.auto_enabled(1))

    async def test_join_while_off_does_nothing(self):
        guild = auto_guild()
        member = RemovableMember(guild, 42, "lemon")
        await self.cog.on_member_join(member)
        self.assertEqual(member.added, [])
        self.assertEqual(guild.created, [])

    async def test_join_while_on_gives_role_hides_channel_and_opens_private_channel(self):
        await self.cog.set_auto_enabled(1, True)
        guild = auto_guild()
        member = RemovableMember(guild, 42, "lemon")
        await self.cog.on_member_join(member)
        self.assertEqual(member.added, [guild.auto_role])
        self.assertFalse(guild.hidden.overwrites_for(member).view_channel)
        self.assertEqual(len(guild.created), 1)
        self.assertIn(member.mention, guild.created[0].sent[0]["content"])
        self.assertEqual(await self.cog.tagged_ids(1), {42})
        self.assertIsNone(await self.cog.last_group(1))
        self.assertEqual((await self.cog.stats(guild))["self_started"], 0)

    async def test_rejoining_member_reuses_existing_channel(self):
        await self.cog.set_auto_enabled(1, True)
        guild = auto_guild()
        await self.cog.on_member_join(RemovableMember(guild, 42, "lemon"))
        await self.cog.on_member_join(RemovableMember(guild, 42, "lemon"))
        self.assertEqual(len(guild.created), 1)

    async def test_finishing_grants_access_and_removes_onboarding_role_and_hiding(self):
        await self.cog.set_auto_enabled(1, True)
        guild = auto_guild()
        member = RemovableMember(guild, 42, "lemon")
        await self.cog.on_member_join(member)
        self.assertTrue(await self.cog.complete(member))
        self.assertIn(guild.access_role, member.roles)
        self.assertNotIn(guild.auto_role, member.roles)
        self.assertEqual(guild.hidden.member_overwrites, {})

    async def test_leaving_before_finishing_clears_the_hidden_override(self):
        await self.cog.set_auto_enabled(1, True)
        guild = auto_guild()
        member = RemovableMember(guild, 42, "lemon")
        await self.cog.on_member_join(member)
        await self.cog.on_member_remove(member)
        self.assertEqual(guild.hidden.member_overwrites, {})

    async def test_auto_problems_report_missing_role_and_channel(self):
        settings = await self.cog.settings(1)
        problems = "\n".join(self.cog.auto_problems(FakeGuild(), settings))
        self.assertIn(str(saferest.AUTO_ONBOARDING_ROLE_ID), problems)
        self.assertIn(str(saferest.AUTO_HIDDEN_CHANNEL_ID), problems)


def interaction_for(cog, *, admin=True):
    return SimpleNamespace(
        user=SimpleNamespace(id=42, bot=False, guild_permissions=SimpleNamespace(manage_guild=admin)),
        guild=SimpleNamespace(id=1),
        client=SimpleNamespace(get_cog=lambda name: cog if name == "SafeRestOnboardingCog" else None),
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock(),
            is_done=lambda: True,
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class AutoOnboardingToggleTests(unittest.IsolatedAsyncioTestCase):
    def toggle_cog(self, enabled=False):
        return SimpleNamespace(
            auto_enabled=AsyncMock(return_value=enabled),
            set_auto_enabled=AsyncMock(),
            settings=AsyncMock(return_value=object()),
            auto_problems=lambda guild, settings: [],
        )

    async def test_only_moderators_can_open_the_switch(self):
        denied = interaction_for(self.toggle_cog(), admin=False)
        await open_auto_onboarding_toggle(denied)
        denied.response.send_message.assert_not_awaited()
        allowed = interaction_for(self.toggle_cog(), admin=True)
        await open_auto_onboarding_toggle(allowed)
        sent = allowed.response.send_message.call_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertIsInstance(sent["view"], AutoOnboardingView)

    async def test_turning_on_and_off_saves_the_setting(self):
        for enabled, button in ((True, "turn_on"), (False, "turn_off")):
            with self.subTest(enabled=enabled):
                cog = self.toggle_cog(not enabled)
                interaction = interaction_for(cog)
                await getattr(AutoOnboardingView(not enabled), button).callback(interaction)
                cog.set_auto_enabled.assert_awaited_once_with(1, enabled)
                edited = interaction.response.edit_message.call_args.kwargs
                self.assertIn("ON" if enabled else "OFF", edited["embed"].description)

    async def test_mod_panel_has_the_switch_and_fits_discord_limits(self):
        panel = ModPanelView()
        self.assertIn("Auto Onboarding", [child.label for child in panel.children])
        for row in range(5):
            self.assertLessEqual(sum(child.row == row for child in panel.children), 5)


if __name__ == "__main__":
    unittest.main()
