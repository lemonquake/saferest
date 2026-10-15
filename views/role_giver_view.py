"""
Role Giver view and management controls for bulk role assignment.
Created by Aljay Leodones for SafeRest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging

import discord

from config import BRAND_NAME
from utils import embed_builder
from views.common import replace_message, require_manage

logger = logging.getLogger("saferest_bot.role_giver")


@dataclass
class RoleGiverStats:
    """Calculated breakdown of members for bulk role assignment."""

    total_members: int = 0
    eligible_members: list[discord.Member] = field(default_factory=list)
    bots: list[discord.Member] = field(default_factory=list)
    admins: list[discord.Member] = field(default_factory=list)
    role_excluded: list[discord.Member] = field(default_factory=list)
    already_has_role: list[discord.Member] = field(default_factory=list)


async def calculate_role_giver_stats(
    guild: discord.Guild,
    target_role: discord.Role | None,
    *,
    exclude_bots: bool = True,
    exclude_admins: bool = True,
    excluded_role_ids: set[int] | None = None,
) -> RoleGiverStats:
    """
    Evaluates server members against the specified target role and filter criteria.
    """
    excluded_role_ids = excluded_role_ids or set()

    # Ensure member cache is populated if possible
    if guild.member_count and len(guild.members) < guild.member_count:
        try:
            if hasattr(guild, "chunked") and not guild.chunked:
                await guild.chunk()
        except Exception as exc:
            logger.warning("Could not chunk guild members: %s", exc)

    stats = RoleGiverStats()
    members = list(guild.members)
    stats.total_members = len(members)

    target_id = target_role.id if target_role else None

    for m in members:
        if exclude_bots and getattr(m, "bot", False):
            stats.bots.append(m)
            continue

        perms = getattr(m, "guild_permissions", None)
        if exclude_admins and perms and perms.administrator:
            stats.admins.append(m)
            continue

        member_role_ids = {r.id for r in getattr(m, "roles", [])}
        if excluded_role_ids and (member_role_ids & excluded_role_ids):
            stats.role_excluded.append(m)
            continue

        if target_id and (target_id in member_role_ids):
            stats.already_has_role.append(m)
            continue

        if target_id:
            stats.eligible_members.append(m)

    return stats


def validate_target_role(
    guild: discord.Guild, target_role: discord.Role | None
) -> tuple[bool, str]:
    """
    Validates whether the target role can be assigned by the bot.
    """
    if target_role is None:
        return False, "No target role selected."

    if target_role.is_default() or target_role.id == guild.id:
        return False, "Cannot assign the default @everyone role."

    if getattr(target_role, "managed", False):
        return (
            False,
            f"**{target_role.name}** is managed by an integration/bot and cannot be assigned.",
        )

    bot_member = guild.me
    if not bot_member:
        return True, "Valid"

    perms = getattr(bot_member, "guild_permissions", None)
    if perms and not perms.manage_roles:
        return False, "The bot lacks the **Manage Roles** permission."

    top_role = getattr(bot_member, "top_role", None)
    if top_role and top_role.position <= target_role.position:
        return False, (
            f"Role hierarchy error: Bot's highest role (**{top_role.name}**) "
            f"must be higher than **{target_role.name}** in Server Settings."
        )

    return True, "Valid"


def build_role_giver_embed(
    guild: discord.Guild,
    target_role: discord.Role | None,
    exclude_bots: bool,
    exclude_admins: bool,
    excluded_roles: list[discord.Role],
    stats: RoleGiverStats | None = None,
    *,
    status_message: str | None = None,
) -> discord.Embed:
    """Builds the main control panel embed for Role Giver."""
    embed = embed_builder.base_embed(
        title=f"{BRAND_NAME} — Role Giver",
        description=(
            f"{embed_builder.SEPARATOR}\n"
            "Bulk-assign a role to all server members with custom exclusion filters.\n"
            "Select the target role and any roles to filter out using the menus below."
        ),
    )
    if status_message:
        embed.description = f"{embed.description}\n\n{status_message}"

    # Target Role field
    if target_role:
        role_members = getattr(target_role, "members", [])
        role_info = [
            embed_builder.item(f"Role: {target_role.mention} (`{target_role.name}`)"),
            embed_builder.item(f"Role ID: `{target_role.id}`"),
            embed_builder.item(f"Position: `{target_role.position}`"),
            embed_builder.item(f"Currently assigned to: **{len(role_members):,}** members"),
        ]
        embed.add_field(
            name=embed_builder.section("Target Role to Give"),
            value="\n".join(role_info),
            inline=False,
        )
    else:
        embed.add_field(
            name=embed_builder.section("Target Role to Give"),
            value=embed_builder.item("⚠️ **No role selected.** Pick one from Step 1 below."),
            inline=False,
        )

    # Filter settings field
    filter_info = [
        embed_builder.item(f"Exclude Bots: **{'✅ Yes (Bots skipped)' if exclude_bots else '❌ No (Bots included)'}**"),
        embed_builder.item(f"Exclude Admins: **{'✅ Yes (Admins skipped)' if exclude_admins else '❌ No (Admins included)'}**"),
    ]
    if excluded_roles:
        role_tags = ", ".join(r.mention for r in excluded_roles)
        filter_info.append(embed_builder.item(f"Excluded Roles ({len(excluded_roles)}): {role_tags}"))
    else:
        filter_info.append(embed_builder.item("Excluded Roles: *None (All roles eligible)*"))

    embed.add_field(
        name=embed_builder.section("Exclusion Filters"),
        value="\n".join(filter_info),
        inline=False,
    )

    # Breakdown field
    if stats and target_role:
        breakdown = [
            embed_builder.item(f"Total Server Members: **{stats.total_members:,}**"),
            embed_builder.item(f"Excluded Bots: **{len(stats.bots):,}**"),
            embed_builder.item(f"Excluded Admins: **{len(stats.admins):,}**"),
            embed_builder.item(f"Excluded by Filter Roles: **{len(stats.role_excluded):,}**"),
            embed_builder.item(f"Already Have Target Role: **{len(stats.already_has_role):,}**"),
            embed_builder.item(f"🎯 **Eligible to Receive Role: {len(stats.eligible_members):,}**"),
        ]
        embed.add_field(
            name=embed_builder.section("Eligibility Breakdown"),
            value="\n".join(breakdown),
            inline=False,
        )
    elif target_role:
        embed.add_field(
            name=embed_builder.section("Eligibility Breakdown"),
            value=embed_builder.item("Click **🔍 Calculate / Preview** to scan members."),
            inline=False,
        )

    # Permission check field
    if target_role:
        is_valid, reason = validate_target_role(guild, target_role)
        status = "✅ Bot has Manage Roles permission & role hierarchy is valid." if is_valid else f"❌ {reason}"
        embed.add_field(
            name=embed_builder.section("Permission Check"),
            value=embed_builder.item(status),
            inline=False,
        )

    return embed


def build_confirm_embed(
    guild: discord.Guild,
    target_role: discord.Role,
    stats: RoleGiverStats,
) -> discord.Embed:
    """Builds the confirmation embed before mass role assignment."""
    embed = embed_builder.warning_embed(
        "Confirm Mass Role Assignment",
        f"You are about to assign {target_role.mention} to **{len(stats.eligible_members):,}** members.\n\n"
        f"**Summary of exclusions:**\n"
        f"{embed_builder.item(f'Bots skipped: **{len(stats.bots):,}**')}\n"
        f"{embed_builder.item(f'Admins skipped: **{len(stats.admins):,}**')}\n"
        f"{embed_builder.item(f'Filter roles skipped: **{len(stats.role_excluded):,}**')}\n"
        f"{embed_builder.item(f'Already had role: **{len(stats.already_has_role):,}**')}\n\n"
        f"Are you sure you want to proceed? This will execute in the background.",
    )
    return embed


class RoleGiverHubView(discord.ui.View):
    """Interactive control hub for selecting target roles and exclusions."""

    def __init__(
        self,
        guild: discord.Guild,
        user: discord.User | discord.Member,
        *,
        target_role: discord.Role | None = None,
        exclude_bots: bool = True,
        exclude_admins: bool = True,
        excluded_roles: list[discord.Role] | None = None,
        timeout: float | None = 600,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.user = user
        self.target_role = target_role
        self.exclude_bots = exclude_bots
        self.exclude_admins = exclude_admins
        self.excluded_roles: list[discord.Role] = excluded_roles or []
        self.stats: RoleGiverStats | None = None
        self._sync_state()

    def _sync_state(self) -> None:
        """Synchronizes button labels and active states."""
        self.toggle_bots_button.label = (
            f"Bots: {'Excluded ✅' if self.exclude_bots else 'Included ❌'}"
        )
        self.toggle_bots_button.style = (
            discord.ButtonStyle.success if self.exclude_bots else discord.ButtonStyle.secondary
        )

        self.toggle_admins_button.label = (
            f"Admins: {'Excluded ✅' if self.exclude_admins else 'Included ❌'}"
        )
        self.toggle_admins_button.style = (
            discord.ButtonStyle.success if self.exclude_admins else discord.ButtonStyle.secondary
        )

        # Give Role button enabled only if valid and members are eligible
        can_give = False
        if self.target_role and self.stats and len(self.stats.eligible_members) > 0:
            is_valid, _ = validate_target_role(self.guild, self.target_role)
            can_give = is_valid
        self.assign_roles_button.disabled = not can_give

    async def _update(
        self, interaction: discord.Interaction, *, recalculate: bool = True
    ) -> None:
        """Recalculates statistics and redraws the view."""
        if recalculate and self.target_role:
            self.stats = await calculate_role_giver_stats(
                self.guild,
                self.target_role,
                exclude_bots=self.exclude_bots,
                exclude_admins=self.exclude_admins,
                excluded_role_ids={r.id for r in self.excluded_roles},
            )
        self._sync_state()
        embed = build_role_giver_embed(
            self.guild,
            self.target_role,
            self.exclude_bots,
            self.exclude_admins,
            self.excluded_roles,
            self.stats,
        )
        await replace_message(interaction, embed, view=self)

    # ── Row 0: Target Role Select ─────────────────────────────
    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Step 1: Choose target role to give...",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def target_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()
        if select.values:
            self.target_role = select.values[0]
        await self._update(interaction, recalculate=True)

    # ── Row 1: Excluded Roles Select ──────────────────────────
    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Step 2: Choose roles to exclude (e.g. VIP, Staff)...",
        min_values=0,
        max_values=25,
        row=1,
    )
    async def exclude_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()
        self.excluded_roles = list(select.values)
        await self._update(interaction, recalculate=True)

    # ── Row 2: Filter Toggle Buttons ──────────────────────────
    @discord.ui.button(
        label="Bots: Excluded ✅",
        style=discord.ButtonStyle.success,
        row=2,
    )
    async def toggle_bots_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()
        self.exclude_bots = not self.exclude_bots
        await self._update(interaction, recalculate=True)

    @discord.ui.button(
        label="Admins: Excluded ✅",
        style=discord.ButtonStyle.success,
        row=2,
    )
    async def toggle_admins_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()
        self.exclude_admins = not self.exclude_admins
        await self._update(interaction, recalculate=True)

    @discord.ui.button(
        label="Clear Excluded Roles",
        style=discord.ButtonStyle.secondary,
        emoji="🧹",
        row=2,
    )
    async def clear_excluded_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()
        self.excluded_roles = []
        await self._update(interaction, recalculate=True)

    # ── Row 3: Action Buttons ─────────────────────────────────
    @discord.ui.button(
        label="Calculate / Preview",
        style=discord.ButtonStyle.primary,
        emoji="🔍",
        row=3,
    )
    async def preview_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()
        await self._update(interaction, recalculate=True)

    @discord.ui.button(
        label="Give Role",
        style=discord.ButtonStyle.success,
        emoji="✨",
        row=3,
        disabled=True,
    )
    async def assign_roles_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        if not self.target_role:
            await interaction.response.send_message(
                "Please select a target role first.", ephemeral=True
            )
            return

        is_valid, reason = validate_target_role(self.guild, self.target_role)
        if not is_valid:
            await interaction.response.send_message(
                f"Cannot assign role: {reason}", ephemeral=True
            )
            return

        if self.stats is None:
            self.stats = await calculate_role_giver_stats(
                self.guild,
                self.target_role,
                exclude_bots=self.exclude_bots,
                exclude_admins=self.exclude_admins,
                excluded_role_ids={r.id for r in self.excluded_roles},
            )

        if not self.stats.eligible_members:
            await interaction.response.send_message(
                "There are no eligible members to give this role to under current filters.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        confirm_view = RoleGiverConfirmView(parent_view=self)
        confirm_embed = build_confirm_embed(self.guild, self.target_role, self.stats)
        await replace_message(interaction, confirm_embed, view=confirm_view)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji="✖️",
        row=3,
    )
    async def close_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        try:
            await interaction.delete_original_response()
        except Exception:
            await interaction.response.edit_message(
                content="*Role Giver closed.*", embed=None, view=None
            )


class RoleGiverConfirmView(discord.ui.View):
    """Confirmation prompt before bulk role assignment."""

    def __init__(self, parent_view: RoleGiverHubView, timeout: float | None = 300):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view

    @discord.ui.button(
        label="Confirm & Assign Role",
        style=discord.ButtonStyle.danger,
        emoji="🚀",
    )
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()

        target_role = self.parent_view.target_role
        stats = self.parent_view.stats
        if not target_role or not stats:
            return

        progress_view = RoleGiverProgressView()
        progress_embed = embed_builder.info_embed(
            "Role Giver — Processing",
            f"Assigning {target_role.mention} to **{len(stats.eligible_members):,}** members...\n\n"
            f"Please wait. Progress updates will appear below.",
        )
        await replace_message(interaction, progress_embed, view=progress_view)

        asyncio.create_task(
            self._execute_assignment(interaction, progress_view, target_role, stats)
        )

    @discord.ui.button(
        label="Back to Settings",
        style=discord.ButtonStyle.secondary,
        emoji="⬅️",
    )
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        await interaction.response.defer()
        self.parent_view._sync_state()
        embed = build_role_giver_embed(
            self.parent_view.guild,
            self.parent_view.target_role,
            self.parent_view.exclude_bots,
            self.parent_view.exclude_admins,
            self.parent_view.excluded_roles,
            self.parent_view.stats,
        )
        await replace_message(interaction, embed, view=self.parent_view)

    async def _execute_assignment(
        self,
        interaction: discord.Interaction,
        progress_view: RoleGiverProgressView,
        target_role: discord.Role,
        stats: RoleGiverStats,
    ) -> None:
        """Executes the role assignment loop with rate-limiting and progress reports."""
        eligible = list(stats.eligible_members)
        total = len(eligible)
        assigned = 0
        failed = 0

        for i, member in enumerate(eligible, start=1):
            if progress_view.cancelled:
                logger.info("Role Giver assignment cancelled by moderator.")
                break

            try:
                await member.add_roles(
                    target_role,
                    reason=f"Role Giver bulk assignment requested by {interaction.user}",
                )
                assigned += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                failed += 1
                logger.warning("Could not add role to member %s: %s", member.id, exc)

            # Update progress every 15 members
            if i % 15 == 0 and not progress_view.cancelled:
                percent = int((i / total) * 100)
                update_embed = embed_builder.info_embed(
                    "Role Giver — In Progress",
                    f"Assigning {target_role.mention} to members...\n\n"
                    f"▸ Progress: **{i}/{total}** members ({percent}%)\n"
                    f"▸ Successful: **{assigned:,}**\n"
                    f"▸ Failed: **{failed:,}**",
                )
                try:
                    await interaction.edit_original_response(
                        embed=update_embed, view=progress_view
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.3)

        # Final result embed
        if progress_view.cancelled:
            result_embed = embed_builder.warning_embed(
                "Role Giver — Stopped Early",
                f"Role assignment was cancelled by user.\n\n"
                f"▸ Assigned to: **{assigned:,}** members\n"
                f"▸ Failed: **{failed:,}**\n"
                f"▸ Remaining: **{total - (assigned + failed):,}**",
            )
        else:
            result_embed = embed_builder.success_embed(
                "Role Giver — Completed",
                f"Successfully assigned {target_role.mention} to **{assigned:,}** members!\n\n"
                + (f"⚠️ **{failed:,}** assignments failed due to Discord API permissions/errors.\n" if failed else "")
                + f"**Summary of exclusions:**\n"
                + f"▸ Bots skipped: **{len(stats.bots):,}**\n"
                + f"▸ Admins skipped: **{len(stats.admins):,}**\n"
                + f"▸ Filter roles skipped: **{len(stats.role_excluded):,}**\n"
                + f"▸ Already had role: **{len(stats.already_has_role):,}**",
            )

        try:
            await interaction.edit_original_response(embed=result_embed, view=None)
        except Exception as exc:
            logger.error("Failed to edit final Role Giver message: %s", exc)


class RoleGiverProgressView(discord.ui.View):
    """View with a cancel button displayed during execution."""

    def __init__(self, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.cancelled = False

    @discord.ui.button(
        label="Cancel Assignment",
        style=discord.ButtonStyle.danger,
        emoji="🛑",
    )
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await require_manage(interaction):
            return
        self.cancelled = True
        button.disabled = True
        button.label = "Cancelling..."
        await interaction.response.edit_message(view=self)


async def open_role_giver(interaction: discord.Interaction) -> None:
    """Opens the Role Giver hub from a button or slash command."""
    if interaction.guild is None:
        await interaction.response.send_message(
            "Role Giver can only be used inside a server.", ephemeral=True
        )
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)

    view = RoleGiverHubView(guild=interaction.guild, user=interaction.user)
    embed = build_role_giver_embed(
        guild=interaction.guild,
        target_role=None,
        exclude_bots=view.exclude_bots,
        exclude_admins=view.exclude_admins,
        excluded_roles=view.excluded_roles,
        stats=None,
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
