"""
Unit tests for the Role Giver module, Mod Panel button, filters, and role assignment logic.
"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from views.mod_panel_view import ModPanelView, FUNCTION_GUIDE
from views.role_giver_view import (
    RoleGiverHubView,
    RoleGiverConfirmView,
    RoleGiverProgressView,
    calculate_role_giver_stats,
    validate_target_role,
    build_role_giver_embed,
    build_confirm_embed,
)


def run(coro):
    return asyncio.run(coro)


class FakeRole:
    def __init__(self, id: int, name: str, position: int = 1, managed: bool = False, is_default: bool = False):
        self.id = id
        self.name = name
        self.position = position
        self.managed = managed
        self._is_default = is_default
        self.members = []

    def is_default(self) -> bool:
        return self._is_default

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"


class FakePermissions:
    def __init__(self, administrator: bool = False, manage_roles: bool = True, manage_guild: bool = True):
        self.administrator = administrator
        self.manage_roles = manage_roles
        self.manage_guild = manage_guild


class FakeMember:
    def __init__(self, id: int, name: str, roles: list[FakeRole] = None, bot: bool = False, admin: bool = False):
        self.id = id
        self.name = name
        self.roles = list(roles or [])
        self.bot = bot
        self.guild_permissions = FakePermissions(administrator=admin)
        self.add_roles = AsyncMock()


class FakeGuild:
    def __init__(self, id: int = 100, members: list[FakeMember] = None, bot_member: FakeMember = None):
        self.id = id
        self.name = "Test Guild"
        self.members = list(members or [])
        self.member_count = len(self.members)
        self.chunked = True
        self.me = bot_member or FakeMember(999, "Bot", roles=[FakeRole(99, "BotRole", position=50)])
        self.chunk = AsyncMock()


class TestRoleGiver(unittest.TestCase):
    def setUp(self):
        self.target_role = FakeRole(1, "TargetRole", position=10)
        self.admin_role = FakeRole(2, "AdminRole", position=20)
        self.vip_role = FakeRole(3, "VIPRole", position=5)

        self.bot_user = FakeMember(10, "OtherBot", bot=True)
        self.admin_member = FakeMember(11, "AdminUser", roles=[self.admin_role], admin=True)
        self.vip_member = FakeMember(12, "VIPUser", roles=[self.vip_role])
        self.existing_member = FakeMember(13, "ExistingUser", roles=[self.target_role])
        self.eligible_member1 = FakeMember(14, "RegularUser1")
        self.eligible_member2 = FakeMember(15, "RegularUser2")

        self.members = [
            self.bot_user,
            self.admin_member,
            self.vip_member,
            self.existing_member,
            self.eligible_member1,
            self.eligible_member2,
        ]
        self.bot_role = FakeRole(99, "BotHighestRole", position=50)
        self.bot_member = FakeMember(999, "OurBot", roles=[self.bot_role])
        self.bot_member.top_role = self.bot_role
        self.bot_member.guild_permissions = FakePermissions(manage_roles=True)

        self.guild = FakeGuild(100, members=self.members, bot_member=self.bot_member)

    def test_calculate_role_giver_stats_all_filters_enabled(self):
        stats = run(
            calculate_role_giver_stats(
                self.guild,
                self.target_role,
                exclude_bots=True,
                exclude_admins=True,
                excluded_role_ids={self.vip_role.id},
            )
        )
        self.assertEqual(stats.total_members, 6)
        self.assertEqual(len(stats.bots), 1)
        self.assertEqual(stats.bots[0].id, self.bot_user.id)
        self.assertEqual(len(stats.admins), 1)
        self.assertEqual(stats.admins[0].id, self.admin_member.id)
        self.assertEqual(len(stats.role_excluded), 1)
        self.assertEqual(stats.role_excluded[0].id, self.vip_member.id)
        self.assertEqual(len(stats.already_has_role), 1)
        self.assertEqual(stats.already_has_role[0].id, self.existing_member.id)
        self.assertEqual(len(stats.eligible_members), 2)
        eligible_ids = {m.id for m in stats.eligible_members}
        self.assertEqual(eligible_ids, {self.eligible_member1.id, self.eligible_member2.id})

    def test_calculate_role_giver_stats_bots_included(self):
        stats = run(
            calculate_role_giver_stats(
                self.guild,
                self.target_role,
                exclude_bots=False,
                exclude_admins=True,
                excluded_role_ids=set(),
            )
        )
        self.assertEqual(len(stats.bots), 0)
        eligible_ids = {m.id for m in stats.eligible_members}
        self.assertIn(self.bot_user.id, eligible_ids)

    def test_calculate_role_giver_stats_admins_included(self):
        stats = run(
            calculate_role_giver_stats(
                self.guild,
                self.target_role,
                exclude_bots=True,
                exclude_admins=False,
                excluded_role_ids=set(),
            )
        )
        self.assertEqual(len(stats.admins), 0)
        eligible_ids = {m.id for m in stats.eligible_members}
        self.assertIn(self.admin_member.id, eligible_ids)

    def test_validate_target_role_none(self):
        valid, reason = validate_target_role(self.guild, None)
        self.assertFalse(valid)
        self.assertIn("No target role", reason)

    def test_validate_target_role_everyone(self):
        everyone = FakeRole(self.guild.id, "@everyone", position=0, is_default=True)
        valid, reason = validate_target_role(self.guild, everyone)
        self.assertFalse(valid)
        self.assertIn("@everyone", reason)

    def test_validate_target_role_managed(self):
        managed = FakeRole(50, "Server Booster", position=10, managed=True)
        valid, reason = validate_target_role(self.guild, managed)
        self.assertFalse(valid)
        self.assertIn("managed", reason)

    def test_validate_target_role_missing_manage_roles_perm(self):
        self.bot_member.guild_permissions = FakePermissions(manage_roles=False)
        valid, reason = validate_target_role(self.guild, self.target_role)
        self.assertFalse(valid)
        self.assertIn("Manage Roles", reason)

    def test_validate_target_role_hierarchy_violation(self):
        # Target role position is higher than bot's top role position
        higher_role = FakeRole(55, "SuperRole", position=100)
        valid, reason = validate_target_role(self.guild, higher_role)
        self.assertFalse(valid)
        self.assertIn("hierarchy", reason)

    def test_validate_target_role_success(self):
        valid, reason = validate_target_role(self.guild, self.target_role)
        self.assertTrue(valid)
        self.assertEqual(reason, "Valid")

    def test_build_embeds(self):
        stats = run(
            calculate_role_giver_stats(
                self.guild,
                self.target_role,
                exclude_bots=True,
                exclude_admins=True,
            )
        )
        embed = build_role_giver_embed(
            self.guild,
            self.target_role,
            exclude_bots=True,
            exclude_admins=True,
            excluded_roles=[self.vip_role],
            stats=stats,
        )
        self.assertIsNotNone(embed)
        self.assertIn("Role Giver", embed.title)

        confirm_embed = build_confirm_embed(self.guild, self.target_role, stats)
        self.assertIsNotNone(confirm_embed)

    def test_mod_panel_has_role_giver_button(self):
        panel = ModPanelView()
        button_map = {child.custom_id: child for child in panel.children if hasattr(child, "custom_id")}
        self.assertIn("saferest_mod_panel:role_giver", button_map)
        rg_btn = button_map["saferest_mod_panel:role_giver"]
        self.assertEqual(rg_btn.label, "Role Giver")
        self.assertEqual(rg_btn.row, 3)

        # Row counts limit check
        for row_idx in range(5):
            count = sum(1 for c in panel.children if getattr(c, "row", None) == row_idx)
            self.assertLessEqual(count, 5, f"Row {row_idx} exceeds 5 buttons limit")

        # Function guide check
        guide_labels = [label for label, _ in FUNCTION_GUIDE]
        self.assertTrue(any("Role Giver" in label for label in guide_labels))

    def test_role_giver_hub_view_component_constraints(self):
        user = FakeMember(100, "Moderator")
        view = RoleGiverHubView(self.guild, user)
        # Check rows
        for row_idx in range(5):
            items_in_row = [c for c in view.children if getattr(c, "row", None) == row_idx]
            # If select menu, at most 1 item
            has_select = any(isinstance(c, (discord.ui.Select, discord.ui.RoleSelect)) for c in items_in_row)
            if has_select:
                self.assertEqual(len(items_in_row), 1)
            else:
                self.assertLessEqual(len(items_in_row), 5)

    def test_role_giver_progress_view_cancellation(self):
        progress_view = RoleGiverProgressView()
        self.assertFalse(progress_view.cancelled)
        interaction = MagicMock()
        interaction.response = MagicMock()
        interaction.response.edit_message = AsyncMock()
        run(progress_view.cancel_button.callback(interaction))
        self.assertTrue(progress_view.cancelled)
        self.assertTrue(progress_view.cancel_button.disabled)


if __name__ == "__main__":
    unittest.main()
