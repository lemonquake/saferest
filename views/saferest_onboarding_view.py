"""
Moderator interface for SafeRest Onboarding (private per-member channels).
Created by Aljay Leodones for SafeRest.

Flow:  Scan  ->  pick the join days  ->  Open onboarding channels.

Auto onboarding:  🚪 Auto Onboarding  ->  Turn ON  ->  every member who joins
gets the onboarding role and their own channel straight away.

Each selected member gets a private ``#<handle>-onboarding`` channel where
they are tagged with the creator questions. The scan screen can also clear
the most recently tagged group and rescan, so those members can be tagged
again (their existing channel is reused, never duplicated).

The member side lives in ``views.creator_onboarding_view`` and is shared
with the public Onboarding post.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import discord

from cogs.saferest_onboarding import (
    AUTO_HIDDEN_CHANNEL_ID,
    AUTO_ONBOARDING_ROLE_ID,
    CATEGORY_LIMIT,
    CategoryFull,
    SafeRestSettings,
    TaggedGroup,
    TicketResult,
)
from cogs.creator_onboarding import SHOP_101_CHECKPOINT_MESSAGE
from cogs.onboarding import DayGroup, missing_permissions, resolve_post_channel
from config import ONBOARDING_LOOKBACK_DAYS
from utils import embed_builder
from views.common import ack, missing_cog, replace_message, require_manage
from views.creator_onboarding_view import OnboardingTicketView, Shop101DoneView

logger = logging.getLogger("saferest_bot.saferest_onboarding_ui")

COMPOSER_TIMEOUT = 900
NOTICE_STYLES = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}
SOURCE_LABELS = {"saved": "", "env": " *(default)*", "unset": ""}
NO_MENTIONS = discord.AllowedMentions.none()
TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
MAX_POSTED_LINES = 10


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog("SafeRestOnboardingCog")


def _category_line(guild: discord.Guild, settings: SafeRestSettings) -> str:
    if not settings.category_id:
        return "*not set*"
    category = guild.get_channel(settings.category_id)
    name = f"**{category.name}**" if category else f"*missing* (`{settings.category_id}`)"
    return name + SOURCE_LABELS.get(settings.sources["category"], "")


def _role_line(settings: SafeRestSettings, key: str = "role") -> str:
    role_id = settings.verified_role_id if key == "verified" else settings.role_id
    if not role_id:
        return "*not set*"
    return f"<@&{role_id}>" + SOURCE_LABELS.get(settings.sources.get(key, "unset"), "")


def _problem_lines(problems: list[str]) -> list[str]:
    return [embed_builder.item(f"⚠️ {problem}") for problem in problems]


# ── Scan composer ──────────────────────────────────────────────────────────
@dataclass(slots=True)
class SafeRestDraft:
    """One moderator's scan, from the member list to the send."""

    user_id: int
    groups: list[DayGroup]
    settings: SafeRestSettings
    last_group: TaggedGroup | None
    problems: list[str]
    selected_keys: set[str] = field(default_factory=lambda: {"all"})
    selected_member_id: int | None = None
    notice: tuple[str, str] | None = None

    def apply_selection(self, keys: set[str]) -> None:
        self.selected_member_id = None
        self.selected_keys = {"all"} if not keys or "all" in keys else keys

    def select_single_member(self, member_id: int | None) -> None:
        self.selected_member_id = member_id

    def is_active(self, key: str) -> bool:
        return "all" in self.selected_keys or key in self.selected_keys

    @property
    def all_pending_members(self) -> list[discord.Member]:
        seen: set[int] = set()
        result: list[discord.Member] = []
        for group in self.groups:
            for member in group.members:
                if member.id not in seen:
                    seen.add(member.id)
                    result.append(member)
        return sorted(
            result,
            key=lambda m: getattr(m, "joined_at", None) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    @property
    def selected_members(self) -> list[discord.Member]:
        if self.selected_member_id is not None:
            for member in self.all_pending_members:
                if member.id == self.selected_member_id:
                    return [member]
            self.selected_member_id = None

        unique: dict[int, discord.Member] = {
            member.id: member
            for group in self.groups
            if self.is_active(group.key)
            for member in group.members
        }
        return sorted(
            unique.values(),
            key=lambda m: getattr(m, "joined_at", None) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    def inform(self, message: str, level: str = "info") -> None:
        self.notice = (level, message)

    def take_notice(self) -> tuple[str, str] | None:
        notice, self.notice = self.notice, None
        return notice


def build_composer_embed(draft: SafeRestDraft, guild: discord.Guild) -> discord.Embed:
    members = draft.selected_members
    lines: list[str] = []
    notice = draft.take_notice()
    if notice:
        level, text = notice
        lines += [f"{NOTICE_STYLES.get(level, 'ℹ️')} {text}", embed_builder.SEPARATOR]

    if draft.groups:
        if draft.selected_member_id is not None and members:
            target = members[0]
            joined_text = (
                f" · Joined <t:{int(target.joined_at.timestamp())}:R>"
                if getattr(target, "joined_at", None)
                else ""
            )
            lines += [
                embed_builder.section("Single member mode") + " **1** member selected",
                embed_builder.item(f"Target: **{target.display_name}** ({target.mention}){joined_text}"),
                embed_builder.item(f"Member ID: `{target.id}`"),
                "",
                "*(Only this single member will have an onboarding channel opened. "
                "Select 'None' in the member menu or pick a join day to switch back)*",
            ]
        else:
            names = [member.display_name for member in members[:12]]
            preview = ", ".join(names) if names else "No members selected yet"
            if len(members) > 12:
                preview += f" … and {len(members) - 12} more"
            lines += [
                embed_builder.section("Members") + f" **{len(members)}** selected",
                preview,
                "",
                "\n".join(
                    embed_builder.item(
                        f"**{group.label}** — {len(group.members)}"
                        + ("  ✅" if draft.is_active(group.key) else "")
                    )
                    for group in draft.groups
                ),
            ]
    else:
        lines.append(
            f"✅ Everyone who joined in the last {ONBOARDING_LOOKBACK_DAYS} days "
            "has been tagged (or already has access)."
        )


    lines += [
        "",
        embed_builder.section("What happens"),
        embed_builder.item(
            "Each member gets a private **#handle-onboarding** channel in "
            f"{_category_line(guild, draft.settings)} — only they and the moderators can see it."
        ),
        embed_builder.item(
            "They're tagged there with the TikTok Shop questions, the rules and the questionnaire."
        ),
        embed_builder.item(
            f"Finishing the questionnaire grants {_role_line(draft.settings)}. "
            "Members who already have it are skipped."
        ),
    ]
    if draft.problems:
        lines += ["", embed_builder.section("Fix before sending"), *_problem_lines(draft.problems)]

    lines.append("")
    if draft.last_group:
        group = draft.last_group
        lines.append(
            embed_builder.section("Last tagged group")
            + f" **{group.count}** member{'s' if group.count != 1 else ''} · "
            f"<t:{int(group.tagged_at)}:R>"
        )
        lines.append(
            "Press **♻️ Clear last tagged group & rescan** to tag them again — "
            "their existing channels are reused."
        )
    else:
        lines.append(embed_builder.section("Last tagged group") + " *none yet*")
    return embed_builder.base_embed(title="🎟️  SafeRest Onboarding", description="\n".join(lines))


async def _draft_for(interaction: discord.Interaction, user_id: int) -> SafeRestDraft:
    cog = _cog(interaction)
    settings = await cog.settings(interaction.guild.id)
    pending = await cog.pending_members(interaction.guild, settings)
    return SafeRestDraft(
        user_id=user_id,
        groups=cog.group_by_day(pending),
        settings=settings,
        last_group=await cog.last_group(interaction.guild.id),
        problems=cog.setup_problems(interaction.guild, settings),
    )


async def render_composer(interaction: discord.Interaction, draft: SafeRestDraft) -> None:
    await replace_message(
        interaction, build_composer_embed(draft, interaction.guild), SafeRestComposerView(draft)
    )


class SafeRestDaySelect(discord.ui.Select):
    def __init__(self, draft: SafeRestDraft):
        total = sum(len(group.members) for group in draft.groups)
        options = [
            discord.SelectOption(
                label=f"Everyone not yet tagged ({total})",
                value="all",
                emoji="✅",
                description=f"All members from the last {ONBOARDING_LOOKBACK_DAYS} days",
                default="all" in draft.selected_keys and draft.selected_member_id is None,
            )
        ]
        for group in draft.groups[:24]:
            options.append(
                discord.SelectOption(
                    label=f"{group.label} ({len(group.members)})",
                    value=group.key,
                    emoji="📅",
                    description=group.description[:100],
                    default=group.key in draft.selected_keys and draft.selected_member_id is None,
                )
            )
        super().__init__(
            placeholder="Which join days to onboard (pick several)...",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        draft: SafeRestDraft = self.view.draft  # type: ignore[attr-defined]
        draft.apply_selection(set(self.values))
        await render_composer(interaction, draft)


class SafeRestSingleMemberSelect(discord.ui.Select):
    """Select a specific single member from the new members list to onboard."""

    def __init__(self, draft: SafeRestDraft):
        pending = draft.all_pending_members
        options = [
            discord.SelectOption(
                label="None (use join days above)",
                value="none",
                emoji="↩️",
                description="Onboard members based on the join days selected above",
                default=draft.selected_member_id is None,
            )
        ]
        for member in pending[:24]:
            joined_str = (
                member.joined_at.strftime("%b %d, %H:%M")
                if getattr(member, "joined_at", None)
                else "recently"
            )
            name_tag = getattr(member, "name", getattr(member, "display_name", str(member.id)))
            disp_name = getattr(member, "display_name", str(member.id))
            options.append(
                discord.SelectOption(
                    label=disp_name[:100],
                    value=str(member.id),
                    emoji="👤",
                    description=f"@{name_tag} · Joined {joined_str}"[:100],
                    default=draft.selected_member_id == member.id,
                )
            )

        super().__init__(
            placeholder="Or onboard a single member from the list...",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        draft: SafeRestDraft = self.view.draft  # type: ignore[attr-defined]
        chosen = self.values[0]
        if chosen == "none":
            draft.select_single_member(None)
        else:
            draft.select_single_member(int(chosen))
        await render_composer(interaction, draft)


class SafeRestComposerMemberPickerView(discord.ui.View):
    """Allows searching and selecting any member for single-member onboarding."""

    def __init__(self, draft: SafeRestDraft, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.draft = draft

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Search for a member to onboard...",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def select_user(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ) -> None:
        if not await require_manage(interaction):
            return
        if not select.values:
            return
        user = select.values[0]
        member = interaction.guild.get_member(user.id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(user.id)
            except (discord.NotFound, discord.HTTPException):
                await interaction.response.send_message(
                    "That member could not be found in this server.", ephemeral=True
                )
                return
        if member.bot:
            await interaction.response.send_message(
                "Bots can't be onboarded.", ephemeral=True
            )
            return

        self.draft.select_single_member(member.id)
        pending_ids = {m.id for m in self.draft.all_pending_members}
        if member.id not in pending_ids:
            self.draft.groups.insert(0, DayGroup("custom", "Selected member", "", [member]))

        self.draft.inform(f"Selected **{member.display_name}** for single-member onboarding.", "success")
        await render_composer(interaction, self.draft)

    @discord.ui.button(label="Back to scan", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await render_composer(interaction, self.draft)


class SafeRestComposerView(discord.ui.View):
    def __init__(self, draft: SafeRestDraft):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.draft = draft
        if draft.groups:
            self.add_item(SafeRestDaySelect(draft))
            if draft.all_pending_members:
                self.add_item(SafeRestSingleMemberSelect(draft))
        count = len(draft.selected_members)
        if draft.selected_member_id is not None and count == 1:
            member = draft.selected_members[0]
            disp = getattr(member, "display_name", str(member.id))
            self.send.label = f"Open onboarding for {disp[:20]}"
        else:
            self.send.label = f"Open {count} onboarding channel{'s' if count != 1 else ''}"

        self.send.disabled = count == 0 or bool(draft.problems)
        self.clear_and_rescan.disabled = draft.last_group is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.draft.user_id:
            await interaction.response.send_message(
                embed=embed_builder.error_embed(
                    "This scan belongs to someone else",
                    "Press **🔍 Scan for new members** to start your own.",
                ),
                ephemeral=True,
            )
            return False
        return await require_manage(interaction)

    @discord.ui.button(label="Open onboarding channels", style=discord.ButtonStyle.success, emoji="🎟️", row=2)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await ack(interaction)
        await _execute_delivery(interaction, self)

    @discord.ui.button(label="Find member", style=discord.ButtonStyle.secondary, emoji="🔍", row=2)
    async def search_member(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        await replace_message(
            interaction,
            embed_builder.base_embed(
                title="🔍  Select a Member to Onboard",
                description=(
                    f"{embed_builder.SEPARATOR}\n"
                    "Search and select any member to onboard individually in a private channel."
                ),
            ),
            SafeRestComposerMemberPickerView(self.draft),
        )

    @discord.ui.button(
        label="Clear last tagged group & rescan",
        style=discord.ButtonStyle.secondary,
        emoji="♻️",
        row=2,
    )
    async def clear_and_rescan(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await ack(interaction)
        cog = _cog(interaction)
        if cog is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        cleared = await cog.clear_last_group(interaction.guild.id)
        draft = await _draft_for(interaction, self.draft.user_id)
        draft.inform(
            f"Cleared **{cleared}** member{'s' if cleared != 1 else ''} from the last tagged "
            "group — anyone who still needs onboarding is back in the list.",
            "success",
        )
        await render_composer(interaction, draft)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️", row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await replace_message(
            interaction, embed_builder.info_embed("Cancelled", "Nothing was sent."), None
        )
        self.stop()


    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        logger.exception("SafeRest onboarding composer control failed", exc_info=error)
        self.draft.inform(
            "That did not go through — the bot console has the details.", "error"
        )
        try:
            await ack(interaction)
            await render_composer(interaction, self.draft)
        except discord.HTTPException:
            logger.warning("Could not report a SafeRest onboarding failure", exc_info=True)


def _result_lines(result: TicketResult) -> list[str]:
    lines = [
        embed_builder.item(f"New onboarding channels: **{len(result.created)}**"),
        embed_builder.item(f"Existing channels re-tagged: **{len(result.reused)}**"),
    ]
    channels = [channel.mention for channel in (result.created + result.reused)[:15]]
    if channels:
        lines.append(embed_builder.item(" ".join(channels)))
    if result.failed:
        lines.append(embed_builder.item(f"Could not open: **{len(result.failed)}**"))
        lines += [
            embed_builder.item(f"{name} — {reason}") for name, reason in result.failed[:10]
        ]
    return lines


async def _execute_delivery(interaction: discord.Interaction, view: SafeRestComposerView) -> None:
    draft = view.draft
    cog = _cog(interaction)
    if cog is None:
        await missing_cog(interaction, "SafeRest Onboarding")
        return
    members = draft.selected_members
    if not members:
        draft.inform("Pick at least one join day first.", "warning")
        await render_composer(interaction, draft)
        return
    settings = await cog.settings(interaction.guild.id)
    problems = cog.setup_problems(interaction.guild, settings)
    if problems:
        draft.problems = problems
        draft.inform("Fix the setup below before sending.", "error")
        await render_composer(interaction, draft)
        return

    def working(done: int = 0) -> discord.Embed:
        return embed_builder.info_embed(
            "Opening onboarding channels...",
            embed_builder.item(f"Done: **{done}/{len(members)}** (about one per second)"),
        )

    # Clearing the controls first means a second click cannot send twice.
    await replace_message(interaction, working(), None)
    view.stop()

    async def progress(done: int, total: int) -> None:
        await interaction.edit_original_response(embeds=[working(done)], view=None)

    try:
        result = await cog.deliver(
            guild=interaction.guild,
            members=members,
            moderator=interaction.user,
            batch_id=interaction.id,
            view_factory=OnboardingTicketView,
            progress=progress,
        )
    except (ValueError, discord.HTTPException) as exc:
        logger.exception("SafeRest onboarding delivery failed")
        embed = embed_builder.error_embed("Could not open the channels", str(exc)[:500])
    else:
        title = "Onboarding channels opened" if result.opened else "No channels opened"
        builder = embed_builder.success_embed if result.opened else embed_builder.error_embed
        embed = builder(title, "\n".join(_result_lines(result)))
    try:
        await interaction.edit_original_response(embeds=[embed], view=None)
    except discord.HTTPException:
        logger.info("SafeRest onboarding finished after the interaction token expired.")


# ── Settings ───────────────────────────────────────────────────────────────
def build_settings_embed(
    guild: discord.Guild, settings: SafeRestSettings, problems: list[str], notice: str | None = None
) -> discord.Embed:
    lines = []
    if notice:
        lines += [f"✅ {notice}", embed_builder.SEPARATOR]
    lines += [
        embed_builder.item(f"**Onboarding category:** {_category_line(guild, settings)}")
        + f"\n*Where the private #handle-onboarding channels are created (max {CATEGORY_LIMIT} per category).*",
        embed_builder.item(f"**Access role:** {_role_line(settings)}")
        + "\n*Granted when a member finishes the questionnaire — unlocks the rest of the server.*",
        embed_builder.item(f"**Verified role:** {_role_line(settings, 'verified')}")
        + "\n*Your server's Verified role.*",
    ]
    if problems:
        lines += ["", embed_builder.section("Needs attention"), *_problem_lines(problems)]
    lines += [
        embed_builder.SEPARATOR,
        "The TikTok Shop 101 link is set in **👋 Onboarding → ⚙️ Settings**.",
    ]
    return embed_builder.base_embed(title="⚙️  SafeRest Onboarding settings", description="\n".join(lines))


async def _render_settings(interaction: discord.Interaction, notice: str | None = None) -> None:
    cog = _cog(interaction)
    settings = await cog.settings(interaction.guild.id)
    problems = cog.setup_problems(interaction.guild, settings)
    await replace_message(
        interaction,
        build_settings_embed(interaction.guild, settings, problems, notice),
        SafeRestSettingsView(settings),
    )


class SafeRestCategorySelect(discord.ui.ChannelSelect):
    def __init__(self, settings: SafeRestSettings):
        super().__init__(
            placeholder="Onboarding category — not set",
            channel_types=[discord.ChannelType.category],
            min_values=0,
            max_values=1,
            default_values=[discord.Object(settings.category_id)] if settings.category_id else [],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        chosen = self.values[0].id if self.values else None
        await _cog(interaction).set_value(interaction.guild.id, "category", chosen)
        await _render_settings(
            interaction, "Onboarding category saved." if chosen else "Category reset to the default."
        )


class SafeRestRoleSelect(discord.ui.RoleSelect):
    def __init__(self, settings: SafeRestSettings):
        super().__init__(
            placeholder="Access role — not set",
            min_values=0,
            max_values=1,
            default_values=[discord.Object(settings.role_id)] if settings.role_id else [],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        chosen = self.values[0].id if self.values else None
        await _cog(interaction).set_value(interaction.guild.id, "role", chosen)
        await _render_settings(
            interaction, f"Access role set to <@&{chosen}>." if chosen else "Role reset to the default."
        )


class SafeRestVerifiedRoleSelect(discord.ui.RoleSelect):
    def __init__(self, settings: SafeRestSettings):
        super().__init__(
            placeholder="Verified role — not set",
            min_values=0,
            max_values=1,
            default_values=(
                [discord.Object(settings.verified_role_id)] if settings.verified_role_id else []
            ),
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        chosen = self.values[0].id if self.values else None
        await _cog(interaction).set_value(interaction.guild.id, "verified", chosen)
        await _render_settings(
            interaction,
            f"Verified role set to <@&{chosen}>." if chosen else "Verified role reset to the default.",
        )


class SafeRestSettingsView(discord.ui.View):
    def __init__(self, settings: SafeRestSettings):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.add_item(SafeRestCategorySelect(settings))
        self.add_item(SafeRestRoleSelect(settings))
        self.add_item(SafeRestVerifiedRoleSelect(settings))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅", row=3)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await replace_message(
            interaction,
            embed_builder.success_embed("Settings saved", "SafeRest Onboarding is ready to use."),
            None,
        )
        self.stop()


async def open_test_channel(interaction: discord.Interaction, member: discord.Member) -> None:
    """
    Opens (or re-tags) a private onboarding channel for one member as a test.

    The member is not added to any tagged group, so scans still list them.
    Replies privately.
    """
    cog = _cog(interaction)
    if cog is None:
        await missing_cog(interaction, "SafeRest Onboarding")
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    settings = await cog.settings(interaction.guild.id)
    problems = cog.setup_problems(interaction.guild, settings)
    category = cog.category(interaction.guild, settings)
    if category is None or any("Manage Channels" in problem for problem in problems):
        await interaction.followup.send(
            embed=embed_builder.error_embed("Fix the setup first", "\n".join(_problem_lines(problems))),
            ephemeral=True,
        )
        return
    try:
        channel, created = await cog.open_ticket(
            member, category, OnboardingTicketView(), batch_id=None, tagged_by=None
        )
    except CategoryFull:
        await interaction.followup.send(
            embed=embed_builder.error_embed(
                "Could not open the test channel",
                f"The **{category.name}** category is full ({CATEGORY_LIMIT} channels).",
            ),
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            embed=embed_builder.error_embed("Could not open the test channel", str(exc)[:500]),
            ephemeral=True,
        )
        return
    yourself = member.id == interaction.user.id
    who = "you" if yourself else member.mention
    lines = [
        embed_builder.item(
            f"{'Opened' if created else 'Re-tagged ' + who + ' in'} {channel.mention}"
            + (" — click through it like a new member would." if yourself else ".")
        ),
        embed_builder.item(
            "Submitting the questionnaire really saves the answers for Slack and grants the access role."
        ),
        embed_builder.item(
            "Not added to any tagged group. Press **🔒 Close onboarding** in the channel when done."
        ),
    ]
    if problems:
        lines += ["", *_problem_lines(problems)]
    await interaction.followup.send(
        embed=embed_builder.success_embed("Test channel ready", "\n".join(lines)),
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )


class SafeRestTestMemberSelectView(discord.ui.View):
    """Allows a moderator to select any member to test the onboarding flow."""

    def __init__(self, timeout: float = 300):
        super().__init__(timeout=timeout)

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Select a member to test onboarding on...",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def select_member(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ) -> None:
        if not await require_manage(interaction):
            return
        if not select.values:
            return
        user = select.values[0]
        member = interaction.guild.get_member(user.id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(user.id)
            except (discord.NotFound, discord.HTTPException):
                await interaction.response.send_message(
                    "That member could not be found in this server.", ephemeral=True
                )
                return
        if member.bot:
            await interaction.response.send_message(
                "Bots can't be onboarded.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_test_channel(interaction, member)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️", row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await replace_message(
            interaction, embed_builder.info_embed("Cancelled", "Test onboarding was cancelled."), None
        )
        self.stop()


# ── Self-serve onboarding ──────────────────────────────────────────────────
def build_self_start_embed() -> discord.Embed:
    """The public card members press to start onboarding on their own."""
    return embed_builder.base_embed(
        title="👋  Start your SafeRest onboarding",
        description="\n".join(
            [
                "Welcome to **SafeRest**! Press **🎟️ Start my onboarding** below and "
                "the bot opens a private channel just for you and the SafeRest team.",
                embed_builder.SEPARATOR,
                embed_builder.item("Answer a few quick questions about TikTok Shop."),
                embed_builder.item("Read the community rules and fill in the short questionnaire."),
                embed_builder.item("Finish it to unlock all of the server's channels."),
                "",
                "Got a question along the way? Just type it in your channel and the team will reply.",
            ]
        ),
    )


SELF_START_REPLIES = {
    "opened": (
        embed_builder.success_embed,
        "Your onboarding channel is ready",
        "Head to {channel} to get started — only you and the SafeRest team can see it.",
    ),
    "existing": (
        embed_builder.info_embed,
        "You already have an onboarding channel",
        "Pick up where you left off in {channel}.",
    ),
    "has_access": (
        embed_builder.info_embed,
        "You're all set",
        "You've already finished onboarding and have access to the server.",
    ),
    "busy": (
        embed_builder.info_embed,
        "Hang on a moment",
        "Your onboarding channel is being opened right now.",
    ),
}
UNAVAILABLE_REPLY = (
    embed_builder.error_embed,
    "Onboarding isn't available right now",
    "Please message the SafeRest team and they'll get you set up.",
)


class SelfOnboardingView(discord.ui.View):
    """Public 🎟️ Start my onboarding button (static custom_id, survives restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start my onboarding",
        style=discord.ButtonStyle.success,
        emoji="🎟️",
        custom_id="saferest_onboarding:self_start",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Onboarding can only be started inside the server.", ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message("Bots can't be onboarded.", ephemeral=True)
            return
        cog = _cog(interaction)
        if cog is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await cog.self_start(member, OnboardingTicketView())
        except discord.HTTPException:
            logger.exception("Self-onboarding failed for member %s", member.id)
            builder, title, text = UNAVAILABLE_REPLY
        else:
            builder, title, text = SELF_START_REPLIES.get(result.status, UNAVAILABLE_REPLY)
            if result.channel is not None:
                text = text.format(channel=result.channel.mention)
        await interaction.followup.send(embed=builder(title, text), ephemeral=True)


def self_post_problems(channel: discord.abc.GuildChannel, guild: discord.Guild) -> tuple[list[str], list[str]]:
    """(what stops the bot posting there, warnings that it may not work as hoped)."""
    blocking = missing_permissions(channel, guild.me)
    if not getattr(channel.permissions_for(guild.me), "embed_links", False):
        blocking.append("Embed Links")
    warnings = []
    if not getattr(channel.permissions_for(guild.default_role), "view_channel", False):
        warnings.append(
            f"@everyone can't see {channel.mention}, so new members only find the button "
            "if another role they have shows them that channel."
        )
    return blocking, warnings


def build_self_post_embed(
    channel_id: int | None,
    posted: list[tuple[str, str]] | None = None,
    notice: str | None = None,
    problems: list[str] | None = None,
) -> discord.Embed:
    lines = []
    if notice:
        lines += [notice, embed_builder.SEPARATOR]
    lines += [
        "Posts a public **🎟️ Start my onboarding** button. Anyone who presses it gets "
        "their own private **#handle-onboarding** channel, exactly like the ones "
        "opened from 🔍 Scan — no moderator needed.",
        "",
        embed_builder.item("Members who already have a channel are pointed back to it."),
        embed_builder.item("Members who already have the access role are told they're all set."),
        embed_builder.item("Post it in as many channels as you like; every copy survives restarts."),
        "",
        embed_builder.item(
            f"Destination: <#{channel_id}>" if channel_id else "Destination: *pick a channel below*"
        ),
    ]
    if problems:
        lines += ["", embed_builder.section("Needs attention"), *_problem_lines(problems)]
    if posted:
        lines += ["", embed_builder.section("Posted this session") + f" **{len(posted)}**"]
        lines += [
            embed_builder.item(f"{mention} · [jump to it]({url})")
            for mention, url in posted[-MAX_POSTED_LINES:]
        ]
    return embed_builder.base_embed(
        title="📣  Post the self-onboarding button", description="\n".join(lines)
    )


class SelfPostChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, channel_id: int | None):
        super().__init__(
            placeholder="Choose the channel to post the button in...",
            channel_types=TEXT_CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            default_values=[discord.Object(channel_id)] if channel_id else [],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SelfPostView = self.view  # type: ignore[assignment]
        await view.show(interaction, self.values[0].id)


class SelfPostView(discord.ui.View):
    """Picks a channel and posts the public self-onboarding button there, repeatably."""

    def __init__(
        self,
        channel_id: int | None = None,
        posted: list[tuple[str, str]] | None = None,
        problems: list[str] | None = None,
    ):
        super().__init__(timeout=COMPOSER_TIMEOUT)
        self.channel_id = channel_id
        self.posted = posted if posted is not None else []
        self.problems = problems or []
        self.add_item(SelfPostChannelSelect(channel_id))
        self.post.disabled = not channel_id or bool(self.problems)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    async def show(
        self, interaction: discord.Interaction, channel_id: int | None, notice: str | None = None
    ) -> None:
        problems = await _self_setup_problems(interaction)
        await replace_message(
            interaction,
            build_self_post_embed(channel_id, self.posted, notice, problems),
            SelfPostView(channel_id, self.posted, problems),
        )

    @discord.ui.button(label="Post button", style=discord.ButtonStyle.success, emoji="📣", row=1)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await ack(interaction)
        channel = await resolve_post_channel(interaction.guild, self.channel_id)
        if channel is None:
            await self.show(interaction, None, "❌ That channel could not be opened. Pick another.")
            return
        blocking, warnings = self_post_problems(channel, interaction.guild)
        if blocking:
            await self.show(
                interaction,
                channel.id,
                f"❌ The bot needs **{', '.join(blocking)}** in {channel.mention}.",
            )
            return
        try:
            message = await channel.send(
                embed=build_self_start_embed(), view=SelfOnboardingView(), allowed_mentions=NO_MENTIONS
            )
        except discord.HTTPException as exc:
            logger.warning("Posting the self-onboarding button failed: %s", exc)
            await self.show(interaction, channel.id, f"❌ Discord rejected the post: {exc}")
            return
        self.posted.append((channel.mention, message.jump_url))
        notice = (
            f"✅ Self-onboarding button posted in {channel.mention}. Press **📣 Post button** "
            "again for another copy, or pick a different channel."
        )
        notice += "".join(f"\n⚠️ {warning}" for warning in warnings)
        await self.show(interaction, channel.id, notice)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, emoji="✖️", row=1)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        count = len(self.posted)
        await replace_message(
            interaction,
            embed_builder.info_embed(
                "Finished",
                f"Posted **{count}** self-onboarding button{'s' if count != 1 else ''}."
                if count
                else "Nothing was posted.",
            ),
            None,
        )
        self.stop()


async def _self_setup_problems(interaction: discord.Interaction) -> list[str]:
    """Setup that would stop members opening their own channel."""
    cog = _cog(interaction)
    settings = await cog.settings(interaction.guild.id)
    return cog.setup_problems(interaction.guild, settings)


async def open_self_onboarding_poster(
    interaction: discord.Interaction, channel_id: int | None = None
) -> None:
    """Privately opens the picker; the moderator chooses where the button goes."""
    if _cog(interaction) is None:
        await missing_cog(interaction, "SafeRest Onboarding")
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    problems = await _self_setup_problems(interaction)
    await interaction.followup.send(
        embed=build_self_post_embed(channel_id, problems=problems),
        view=SelfPostView(channel_id, problems=problems),
        ephemeral=True,
    )


# ── Auto onboarding switch ─────────────────────────────────────────────────
def auto_onboarding_embed(enabled: bool, problems: list[str] | None = None) -> discord.Embed:
    state = "🟢 **ON**" if enabled else "🔴 **OFF**"
    description = (
        f"Auto onboarding is {state}.\n\n"
        "While it is ON, every member who joins automatically gets the "
        f"<@&{AUTO_ONBOARDING_ROLE_ID}> role, loses sight of <#{AUTO_HIDDEN_CHANNEL_ID}>, "
        "and gets their own private onboarding channel. When they finish, they "
        "get the access role, the onboarding role is removed and the hidden "
        "channel comes back.\n\n"
        "While it is OFF, nothing happens when members join."
    )
    if enabled and problems:
        description += "\n\n⚠️ **Fix these, or new members won't be onboarded properly:**\n"
        description += "\n".join(f"• {problem}" for problem in problems)
    return embed_builder.base_embed(title="Auto Onboarding", description=description)


class AutoOnboardingView(discord.ui.View):
    """A private moderator switch; turning it ON or OFF takes effect at once."""

    def __init__(self, enabled: bool):
        super().__init__(timeout=300)
        self.turn_on.disabled = enabled
        self.turn_off.disabled = not enabled

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    async def _apply(self, interaction: discord.Interaction, enabled: bool) -> None:
        cog = _cog(interaction)
        if cog is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        await cog.set_auto_enabled(interaction.guild.id, enabled)
        logger.info(
            "Auto onboarding turned %s in %s by %s",
            "on" if enabled else "off", interaction.guild.id, interaction.user.id,
        )
        problems = cog.auto_problems(interaction.guild, await cog.settings(interaction.guild.id))
        await interaction.response.edit_message(
            embed=auto_onboarding_embed(enabled, problems), view=AutoOnboardingView(enabled)
        )

    @discord.ui.button(label="Turn ON", style=discord.ButtonStyle.success, emoji="🟢")
    async def turn_on(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, True)

    @discord.ui.button(label="Turn OFF", style=discord.ButtonStyle.danger, emoji="🔴")
    async def turn_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, False)


async def open_auto_onboarding_toggle(interaction: discord.Interaction) -> None:
    if not await require_manage(interaction):
        return
    cog = _cog(interaction)
    if cog is None:
        await missing_cog(interaction, "SafeRest Onboarding")
        return
    enabled = await cog.auto_enabled(interaction.guild.id)
    problems = cog.auto_problems(interaction.guild, await cog.settings(interaction.guild.id))
    await interaction.response.send_message(
        embed=auto_onboarding_embed(enabled, problems),
        view=AutoOnboardingView(enabled),
        ephemeral=True,
    )


# ── Persistent hub ─────────────────────────────────────────────────────────
async def build_saferest_hub_embed(interaction: discord.Interaction) -> discord.Embed:
    cog = _cog(interaction)
    if cog is None:
        return embed_builder.error_embed(
            "SafeRest Onboarding unavailable", "The SafeRest onboarding module did not load."
        )
    guild = interaction.guild
    settings = await cog.settings(guild.id)
    stats = await cog.stats(guild)
    problems = cog.setup_problems(guild, settings)
    auto_on = await cog.auto_enabled(guild.id)
    creator = interaction.client.get_cog("CreatorOnboardingCog")
    answers = await creator.stats(guild) if creator else None

    blocks = [
        "Give every new member a private **#handle-onboarding** channel, tag them "
        "there with the TikTok Shop questions, the community rules and the "
        "questionnaire. Finishing it unlocks the rest of the server.",
        embed_builder.SEPARATOR,
        embed_builder.section("Right now"),
        "\n".join(
            [
                embed_builder.item(f"Auto onboarding: **{'ON' if auto_on else 'OFF'}**"),
                embed_builder.item(f"Waiting to be tagged: **{stats['pending']}**"),
                embed_builder.item(f"Open onboarding channels: **{stats['channels']}**"),
                embed_builder.item(f"Started it themselves: **{stats['self_started']}**"),
                embed_builder.item(f"Finished onboarding: **{stats['completed']}**"),
            ]
        ),
    ]
    if answers:
        blocks += [
            "",
            embed_builder.section("Answers (all onboarding posts)"),
            embed_builder.item(
                f"✅ **{answers['yes']}** · 🌱 **{answers['learning']}** · "
                f"🔥 **{answers['experienced']}** · ❌ **{answers['no']}**"
            ),
        ]
    blocks += [
        "",
        embed_builder.section("Setup"),
        embed_builder.item(f"**Category:** {_category_line(guild, settings)}"),
        embed_builder.item(f"**Access role:** {_role_line(settings)}"),
        *_problem_lines(problems),
        "",
        embed_builder.section("How to use it"),
        embed_builder.item(
            "**🧪 Test on myself / member** (or `/test-onboarding`) opens a test channel to click through."
        ),
        embed_builder.item(
            "**🔒 Close onboarding** in a channel lets the member or any moderator delete it."
        ),
        embed_builder.item(
            "**🔍 Scan** groups recent members by join day — or select a **single member** to onboard individually."
        ),
        embed_builder.item(
            "**🚪 Auto onboarding** turns it ON or OFF: while ON, every member who "
            "joins gets their own private onboarding channel straight away."
        ),
        embed_builder.item(
            "**📌 Post Shop 101 checkpoint** adds an *I've finished TikTok Shop 101* "
            "button to that channel, so 🌱 creators can continue from there."
        ),
    ]
    return embed_builder.base_embed(title="🎟️  SafeRest Onboarding", description="\n".join(blocks))


class SafeRestOnboardingHubView(discord.ui.View):
    """Persistent SafeRest Onboarding hub (static custom_ids, survives restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Scan for new members",
        style=discord.ButtonStyle.success,
        custom_id="saferest_onboarding_hub:scan",
        emoji="🔍",
        row=0,
    )
    async def scan(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        if _cog(interaction) is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        draft = await _draft_for(interaction, interaction.user.id)
        await interaction.followup.send(
            embed=build_composer_embed(draft, interaction.guild),
            view=SafeRestComposerView(draft),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @discord.ui.button(
        label="Onboard single member",
        style=discord.ButtonStyle.success,
        custom_id="saferest_onboarding_hub:single_member",
        emoji="👤",
        row=0,
    )
    async def single_member(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        if _cog(interaction) is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        draft = await _draft_for(interaction, interaction.user.id)
        if not draft.all_pending_members:
            await interaction.followup.send(
                embed=embed_builder.info_embed(
                    "No pending new members",
                    f"Everyone who joined in the last {ONBOARDING_LOOKBACK_DAYS} days "
                    "has already been tagged or finished onboarding.",
                ),
                ephemeral=True,
            )
            return
        draft.select_single_member(draft.all_pending_members[0].id)
        draft.inform(
            f"Selected **{draft.all_pending_members[0].display_name}**. Use the dropdown to choose another member.",
            "info",
        )
        await interaction.followup.send(
            embed=build_composer_embed(draft, interaction.guild),
            view=SafeRestComposerView(draft),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @discord.ui.button(
        label="Test on myself",
        style=discord.ButtonStyle.primary,
        custom_id="saferest_onboarding_hub:test",
        emoji="🧪",
        row=0,
    )
    async def test(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        cog = _cog(interaction)
        if cog is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_test_channel(interaction, interaction.user)

    @discord.ui.button(
        label="Test on member",
        style=discord.ButtonStyle.primary,
        custom_id="saferest_onboarding_hub:test_member",
        emoji="🧪",
        row=0,
    )
    async def test_member(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        cog = _cog(interaction)
        if cog is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        embed = embed_builder.base_embed(
            title="🧪  Test SafeRest Onboarding on a Member",
            description=(
                f"{embed_builder.SEPARATOR}\n"
                "Select any member from the menu below to open a private test onboarding "
                "channel for them without adding them to a tagged group.\n\n"
                f"{embed_builder.item('A private **#<handle>-onboarding** channel will be opened.')}\n"
                f"{embed_builder.item('They will be tagged with the creator questions.')}\n"
                f"{embed_builder.item('Use **🔒 Close onboarding** inside the channel when finished.')}"
            ),
        )
        await interaction.response.send_message(
            embed=embed, view=SafeRestTestMemberSelectView(), ephemeral=True
        )

    @discord.ui.button(
        label="Post Shop 101 checkpoint",
        style=discord.ButtonStyle.secondary,
        custom_id="saferest_onboarding_hub:checkpoint",
        emoji="📌",
        row=1,
    )
    async def checkpoint(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        creator = interaction.client.get_cog("CreatorOnboardingCog")
        if creator is None:
            await missing_cog(interaction, "Creator onboarding")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel_id = (await creator.settings(interaction.guild.id)).channels.get("getting_started")
        channel = await creator.resolve_channel(interaction.guild, channel_id)
        if channel is None:
            await interaction.followup.send(
                embed=embed_builder.error_embed(
                    "TikTok Shop 101 channel not found",
                    "Set it in **👋 Onboarding → ⚙️ Settings → Getting-started channel**.",
                ),
                ephemeral=True,
            )
            return
        missing = missing_permissions(channel, interaction.guild.me)
        if missing:
            await interaction.followup.send(
                embed=embed_builder.error_embed(
                    "Missing permissions",
                    f"The bot needs **{', '.join(missing)}** in {channel.mention}.",
                ),
                ephemeral=True,
            )
            return
        message = await channel.send(
            SHOP_101_CHECKPOINT_MESSAGE, view=Shop101DoneView(), allowed_mentions=NO_MENTIONS
        )
        await interaction.followup.send(
            embed=embed_builder.success_embed(
                "Checkpoint posted",
                embed_builder.item(f"{channel.mention} · [jump to it]({message.jump_url})")
                + "\n"
                + embed_builder.item(
                    "Members who click it get the rules and the questionnaire privately."
                ),
            ),
            ephemeral=True,
        )

    # The custom_id stays "self_start_post" so hubs posted before the switch
    # replaced the channel picker keep working.
    @discord.ui.button(
        label="Auto onboarding",
        style=discord.ButtonStyle.success,
        custom_id="saferest_onboarding_hub:self_start_post",
        emoji="🚪",
        row=1,
    )
    async def self_start_post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_auto_onboarding_toggle(interaction)

    @discord.ui.button(
        label="Settings",
        style=discord.ButtonStyle.primary,
        custom_id="saferest_onboarding_hub:settings",
        emoji="⚙️",
        row=1,
    )
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        cog = _cog(interaction)
        if cog is None:
            await missing_cog(interaction, "SafeRest Onboarding")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await cog.settings(interaction.guild.id)
        problems = cog.setup_problems(interaction.guild, settings)
        await interaction.followup.send(
            embed=build_settings_embed(interaction.guild, settings, problems),
            view=SafeRestSettingsView(settings),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Stats",
        style=discord.ButtonStyle.secondary,
        custom_id="saferest_onboarding_hub:stats",
        emoji="📋",
        row=1,
    )
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await require_manage(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embed=await build_saferest_hub_embed(interaction), ephemeral=True)


__all__ = [
    "SafeRestComposerMemberPickerView",
    "SafeRestComposerView",
    "SafeRestDraft",
    "SafeRestOnboardingHubView",
    "SafeRestSettingsView",
    "SafeRestSingleMemberSelect",
    "SafeRestTestMemberSelectView",
    "AutoOnboardingView",
    "SelfOnboardingView",
    "SelfPostView",
    "build_saferest_hub_embed",
    "build_composer_embed",
    "build_self_post_embed",
    "build_self_start_embed",
    "build_settings_embed",
    "open_auto_onboarding_toggle",
    "open_self_onboarding_poster",
    "open_test_channel",
]

