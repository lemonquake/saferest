"""
Embed Studio: compose, save, reuse and publish branded embeds.
Created by Aljay Leodones for SafeRest.

The studio is one ephemeral hub message that always shows the current draft.
Every change is written straight back to SQLite — the draft to `embed_drafts`
and named templates to `embed_templates` — so nothing depends on the panel
staying open. Close the window, let it time out or restart the bot: reopening
the studio restores exactly where the moderator left off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Optional

import discord

from core.embed_script import PLACEHOLDER_HELP, EmbedScript
from core.embed_templates import (
    MAX_NAME_LENGTH,
    MAX_TEMPLATES_PER_GUILD,
    DuplicateTemplateError,
    EmbedTemplate,
    TemplateError,
    TemplateStore,
    get_template_store,
)
from utils import embed_builder
from views.banner import BannerModal, describe_banner
from views.broadcast_view import EmbedComposerModal, show_preview
from views.common import ack, replace_message, require_manage

logger = logging.getLogger("saferest_bot.embed_studio")

# Discord allows 25 options per select menu.
PAGE_SIZE = 25
STUDIO_TIMEOUT = 900  # 15 minutes of inactivity
FIELD_LIMIT = 1024


def _shorten(text: str | None, limit: int, *, fallback: str = "*not set*") -> str:
    """One-line summary of a field, with real line breaks made visible."""
    if not text:
        return fallback
    flat = text.replace("\n", " ⏎ ")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _fit(text: str, limit: int) -> str:
    """Keeps a composed block inside an embed limit, line breaks intact."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _measure(text: str | None) -> str:
    """Describes a body of text instead of mangling it into one line."""
    if not text:
        return "*not set*"
    lines = text.count("\n") + 1
    return (
        f"**{len(text):,}** characters"
        + (f" over **{lines}** lines" if lines > 1 else "")
        + " — shown in full below"
    )


def _stamp(value: str | None) -> str:
    """`2026-09-04 11:20:31` -> `2026-09-04 11:20 UTC`."""
    if not value:
        return "unknown"
    return f"{value[:16]} UTC"


def _author(user_id: int | None) -> str:
    return f"<@{user_id}>" if user_id else "unknown"


# ── studio state ───────────────────────────────────────────────────────────
@dataclass(slots=True)
class StudioState:
    """
    Everything the hub needs to redraw itself.

    Held on the ephemeral view only; the draft itself lives in the database,
    so losing this object never loses work.
    """

    guild_id: int
    user_id: int
    script: EmbedScript
    templates: list[EmbedTemplate] = field(default_factory=list)
    page: int = 0
    selected_id: Optional[int] = None
    saved_payload: Optional[dict] = None
    notice: Optional[str] = None
    notice_level: str = "info"

    @property
    def store(self) -> TemplateStore:
        return get_template_store()

    # ── template list paging ──────────────────────────────────
    @property
    def pages(self) -> int:
        return max(1, -(-len(self.templates) // PAGE_SIZE))

    def clamp_page(self) -> None:
        self.page = min(max(self.page, 0), self.pages - 1)

    def page_templates(self) -> list[EmbedTemplate]:
        self.clamp_page()
        start = self.page * PAGE_SIZE
        return self.templates[start : start + PAGE_SIZE]

    def focus(self, template_id: int | None) -> None:
        """Turns the picker to the page holding `template_id`."""
        if template_id is None:
            return
        for index, template in enumerate(self.templates):
            if template.template_id == template_id:
                self.page = index // PAGE_SIZE
                return

    def selected(self) -> EmbedTemplate | None:
        return next(
            (t for t in self.templates if t.template_id == self.selected_id), None
        )

    # ── draft status ──────────────────────────────────────────
    @property
    def linked(self) -> bool:
        return self.script.template_id is not None

    @property
    def unsaved_changes(self) -> bool:
        if not self.linked:
            return False
        return self.saved_payload != self.script.to_payload()

    def link_to(self, template: EmbedTemplate) -> None:
        self.script.template_id = template.template_id
        self.script.template_name = template.name
        self.saved_payload = self.script.to_payload()

    def unlink(self) -> None:
        self.script.template_id = None
        self.script.template_name = None
        self.saved_payload = None

    # ── notices ───────────────────────────────────────────────
    def inform(self, message: str, level: str = "info") -> None:
        self.notice = message
        self.notice_level = level

    def take_notice(self) -> tuple[str | None, str]:
        notice, level = self.notice, self.notice_level
        self.notice, self.notice_level = None, "info"
        return notice, level

    # ── persistence (never fatal to the UI) ───────────────────
    async def reload_templates(self) -> None:
        try:
            self.templates = await self.store.list_templates(self.guild_id)
        except TemplateError as exc:
            logger.warning("Template list unavailable: %s", exc)
            self.inform(str(exc), "warning")
        self.clamp_page()

    async def autosave(self) -> None:
        try:
            await self.store.save_draft(
                guild_id=self.guild_id, user_id=self.user_id, script=self.script
            )
        except TemplateError:
            logger.warning("Draft auto-save skipped for user %s", self.user_id)


# ── hub embed ──────────────────────────────────────────────────────────────
def build_studio_embed(state: StudioState, guild: discord.Guild | None) -> discord.Embed:
    script = state.script
    notice, level = state.take_notice()

    intro = [
        "Write the message once, save it as a template, and reuse it any time.",
        "Templates are stored on the server and shared with the whole team.",
    ]
    if script.has_embed:
        intro.append(
            "The card below is your draft as Discord will render it. "
            "Placeholders are filled in when you publish."
        )
    if notice:
        badge = {
            "success": embed_builder.BADGE_HEALTHY,
            "warning": embed_builder.BADGE_WARNING,
        }.get(level, "ℹ️")
        intro.insert(0, f"{badge}  {notice}\n")

    embed = embed_builder.base_embed(
        title="📝  Embed Studio",
        description="\n".join(intro) + f"\n{embed_builder.SEPARATOR}",
        color=script.color,
    )

    # Only single-line values are summarised here. The message itself keeps
    # its headings, line breaks and emphasis in the draft card below, so it is
    # described by size rather than squashed into one line.
    draft_lines = [
        embed_builder.item(f"**Title:** {_shorten(script.title, 200)}"),
        embed_builder.item(f"**Message:** {_measure(script.description)}"),
        embed_builder.item(
            "**Banner:** " + describe_banner(script, missing="*none*")
        ),
        embed_builder.item(f"**Colour:** `#{script.color:06X}`"),
        embed_builder.item(
            "**Server icon:** "
            + ("shown" if script.icon_visible else "hidden")
        ),
    ]
    embed.add_field(
        name=embed_builder.section("Current draft"),
        value=_fit("\n".join(draft_lines), FIELD_LIMIT),
        inline=False,
    )

    if script.content:
        # Line breaks survive inside a field value, so the plain line that
        # goes above the embed is shown exactly as it was typed.
        embed.add_field(
            name=embed_builder.section("Above the embed"),
            value=_fit(script.content, FIELD_LIMIT),
            inline=False,
        )

    if state.linked and state.unsaved_changes:
        template_state = embed_builder.item(
            f"Editing **{script.template_name}** — "
            f"{embed_builder.BADGE_WARNING} unsaved changes "
            "(use **Update template** to store them)"
        )
    elif state.linked:
        template_state = embed_builder.item(
            f"Editing **{script.template_name}** — "
            f"{embed_builder.BADGE_HEALTHY} saved"
        )
    else:
        template_state = embed_builder.item(
            "This draft is not saved as a template yet."
        )

    embed.add_field(
        name=embed_builder.section("Template"),
        value="\n".join(
            [
                template_state,
                embed_builder.item(
                    f"Saved on this server: **{len(state.templates)}** / "
                    f"{MAX_TEMPLATES_PER_GUILD}"
                ),
                embed_builder.item(
                    "Your draft is auto-saved — closing this window keeps it."
                ),
            ]
        ),
        inline=False,
    )

    embed.add_field(
        name=embed_builder.section("Placeholders you can use"),
        value=f"`{PLACEHOLDER_HELP}`",
        inline=False,
    )
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def build_studio_embeds(
    state: StudioState, guild: discord.Guild | None
) -> list[discord.Embed]:
    """
    The hub card, followed by the draft rendered as a real embed.

    Discord shows several embeds on one message in order, so the second card
    is the draft itself — same title, body, colour, image and thumbnail the
    published message will carry, with every heading and line break intact.
    Placeholders stay unresolved on purpose: this is the text as typed, and
    **Preview & publish** is where they are filled in live.
    """
    embeds = [build_studio_embed(state, guild)]
    draft = state.script.render_embed(guild=guild, resolve=False)
    if draft is not None:
        embeds.append(draft)
    return embeds


def build_manage_embed(state: StudioState) -> discord.Embed:
    notice, level = state.take_notice()
    lines: list[str] = []
    if notice:
        badge = {
            "success": embed_builder.BADGE_HEALTHY,
            "warning": embed_builder.BADGE_WARNING,
        }.get(level, "ℹ️")
        lines.append(f"{badge}  {notice}\n")

    if not state.templates:
        lines.append(
            "No templates saved yet. Compose a message in the studio and use "
            "**Save as template**."
        )
    else:
        lines.append(
            "Pick a template to rename, delete or load it into the studio."
        )
        lines.append(embed_builder.SEPARATOR)
        for template in state.page_templates():
            marker = (
                f" {embed_builder.BADGE_WARNING} unreadable" if template.damaged else ""
            )
            lines.append(
                embed_builder.item(
                    f"**{template.name}**{marker}\n"
                    f"　used **{template.use_count}**× · updated "
                    f"{_stamp(template.updated_at)} by {_author(template.updated_by)}"
                )
            )

    embed = embed_builder.base_embed(
        title="🗂️  Template library",
        description=_fit("\n".join(lines), 4000),
    )
    embed.add_field(
        name=embed_builder.section("Library"),
        value=embed_builder.item(
            f"**{len(state.templates)}** of {MAX_TEMPLATES_PER_GUILD} slots used"
            + (f" · page **{state.page + 1}/{state.pages}**" if state.pages > 1 else "")
        ),
        inline=False,
    )
    return embed


# ── rendering helpers ──────────────────────────────────────────────────────
async def _replace(
    interaction: discord.Interaction,
    embed: discord.Embed | list[discord.Embed],
    view: discord.ui.View | None,
    attachments: list[discord.File] | None = None,
) -> None:
    await replace_message(interaction, embed, view, attachments=attachments)


async def render_studio(interaction: discord.Interaction, state: StudioState) -> None:
    await _replace(
        interaction,
        build_studio_embeds(state, interaction.guild),
        EmbedStudioView(state),
        # The draft card points at `attachment://…` when the banner was
        # uploaded, so the file has to be on the message for it to show.
        attachments=state.script.render_files(),
    )


async def render_manage(interaction: discord.Interaction, state: StudioState) -> None:
    # The template list carries no banner, so any staged upload is cleared
    # off the message while this screen is open.
    await _replace(
        interaction, build_manage_embed(state), TemplateManageView(state), attachments=[]
    )


async def open_studio(interaction: discord.Interaction) -> None:
    """
    Entry point used by the Mod Panel.

    Restores the moderator's auto-saved draft and the server's template list
    before the hub is shown, so the studio always opens with real state.
    """
    if interaction.guild is None:
        await interaction.followup.send(
            embed=embed_builder.error_embed(
                "Server only",
                "The Embed Studio has to be opened from inside the server.",
            ),
            ephemeral=True,
        )
        return

    store = get_template_store()
    script: EmbedScript | None = None
    notice: tuple[str, str] | None = None
    try:
        script = await store.load_draft(
            guild_id=interaction.guild.id, user_id=interaction.user.id
        )
    except TemplateError as exc:
        notice = (str(exc), "warning")

    state = StudioState(
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
        script=script or EmbedScript(interaction.user.id),
    )
    await state.reload_templates()

    if script is not None:
        state.focus(script.template_id)
        if script.template_id is not None:
            # Compare the restored draft against what the template holds now,
            # so "unsaved changes" is accurate after a restart — and pick up a
            # rename that happened while the draft was put away.
            try:
                template = await store.get(state.guild_id, script.template_id)
                state.saved_payload = template.to_script(state.user_id).to_payload()
                state.script.template_name = template.name
            except TemplateError:
                state.unlink()
        if state.notice is None:  # never bury a warning from the list load
            state.inform("Restored your saved draft.", "success")
    if notice:
        state.inform(*notice)

    await interaction.followup.send(
        embeds=build_studio_embeds(state, interaction.guild),
        view=EmbedStudioView(state),
        files=state.script.render_files(),
        ephemeral=True,
    )


# ── template picker ────────────────────────────────────────────────────────
class TemplateLoadSelect(discord.ui.Select):
    """Loads a saved template into the draft in one click."""

    def __init__(self, state: StudioState, row: int = 0):
        self.state = state
        page = state.page_templates()
        options = [
            discord.SelectOption(
                label=template.name[:100],
                value=str(template.template_id),
                description=template.preview_line()[:100],
                emoji="⚠️" if template.damaged else "📄",
                default=template.template_id == state.script.template_id,
            )
            for template in page
        ]
        placeholder = "📂  Load a saved template..."
        if state.pages > 1:
            placeholder = (
                f"📂  Load a saved template (page {state.page + 1}/{state.pages})..."
            )
        if not options:
            options = [discord.SelectOption(label="Nothing saved yet", value="0")]
            placeholder = "📂  No templates saved yet — save your first below"
        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=1,
            row=row,
            disabled=not page,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        try:
            template = await state.store.get(state.guild_id, int(self.values[0]))
        except TemplateError as exc:
            state.inform(str(exc), "warning")
            await state.reload_templates()
            await render_studio(interaction, state)
            return

        state.script = template.to_script(state.user_id)
        state.saved_payload = state.script.to_payload()
        state.selected_id = template.template_id
        await state.store.mark_used(
            guild_id=state.guild_id, template_id=template.template_id
        )
        await state.autosave()
        state.inform(f"Loaded **{template.name}**.", "success")
        await render_studio(interaction, state)


class TemplateManageSelect(discord.ui.Select):
    """Chooses which template the manage buttons act on."""

    def __init__(self, state: StudioState, row: int = 0):
        self.state = state
        page = state.page_templates()
        options = [
            discord.SelectOption(
                label=template.name[:100],
                value=str(template.template_id),
                description=template.preview_line()[:100],
                emoji="⚠️" if template.damaged else "📄",
                default=template.template_id == state.selected_id,
            )
            for template in page
        ]
        if not options:
            options = [discord.SelectOption(label="Nothing saved yet", value="0")]
        super().__init__(
            placeholder="Select a template to work with...",
            options=options,
            min_values=1,
            max_values=1,
            row=row,
            disabled=not page,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.selected_id = int(self.values[0])
        await render_manage(interaction, self.state)


class PageButton(discord.ui.Button):
    """Steps the template picker one page in either direction."""

    def __init__(self, state: StudioState, step: int, row: int, *, manage: bool):
        self.state = state
        self.step = step
        self.manage = manage
        super().__init__(
            label="Previous" if step < 0 else "Next",
            emoji="◀️" if step < 0 else "▶️",
            style=discord.ButtonStyle.secondary,
            row=row,
            disabled=(
                state.page == 0 if step < 0 else state.page >= state.pages - 1
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.page += self.step
        self.state.clamp_page()
        if self.manage:
            await render_manage(interaction, self.state)
        else:
            await render_studio(interaction, self.state)


# ── modals ─────────────────────────────────────────────────────────────────
class TemplateNameModal(discord.ui.Modal, title="Save as template"):
    """Names the draft and stores it."""

    def __init__(self, state: StudioState):
        super().__init__()
        self.state = state
        self.template_name = discord.ui.TextInput(
            label="Template name",
            placeholder="Example: Weekly restock announcement",
            default=state.script.template_name,
            max_length=MAX_NAME_LENGTH,
            required=True,
        )
        self.add_item(self.template_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        typed = self.template_name.value
        try:
            result = await state.store.save(
                guild_id=state.guild_id,
                name=typed,
                script=state.script,
                actor_id=interaction.user.id,
            )
        except DuplicateTemplateError as exc:
            await _replace(
                interaction,
                embed_builder.warning_embed(
                    "Name already used",
                    "\n".join(
                        [
                            f"**{exc.template.name}** was last updated "
                            f"{_stamp(exc.template.updated_at)} by "
                            f"{_author(exc.template.updated_by)}.",
                            "",
                            "Replace it with the current draft, or go back and "
                            "pick a different name.",
                        ]
                    ),
                ),
                OverwriteConfirmView(state, typed, exc.template),
            )
            return
        except TemplateError as exc:
            state.inform(str(exc), "warning")
            await render_studio(interaction, state)
            return

        state.link_to(result.template)
        await state.reload_templates()
        state.focus(result.template.template_id)
        state.selected_id = result.template.template_id
        await state.autosave()
        state.inform(
            f"Saved as **{result.template.name}**."
            if result.created
            else f"Replaced **{result.template.name}** with this draft.",
            "success",
        )
        await render_studio(interaction, state)


class TemplateRenameModal(discord.ui.Modal, title="Rename template"):
    def __init__(self, state: StudioState, template: EmbedTemplate):
        super().__init__()
        self.state = state
        self.template = template
        self.template_name = discord.ui.TextInput(
            label="New name",
            default=template.name,
            max_length=MAX_NAME_LENGTH,
            required=True,
        )
        self.add_item(self.template_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_manage(interaction):
            return
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        try:
            renamed = await state.store.rename(
                guild_id=state.guild_id,
                template_id=self.template.template_id,
                name=self.template_name.value,
                actor_id=interaction.user.id,
            )
        except TemplateError as exc:
            state.inform(str(exc), "warning")
            await render_manage(interaction, state)
            return

        if state.script.template_id == renamed.template_id:
            state.script.template_name = renamed.name
        await state.reload_templates()
        state.focus(renamed.template_id)
        state.inform(f"Renamed to **{renamed.name}**.", "success")
        await render_manage(interaction, state)


# ── confirmations ──────────────────────────────────────────────────────────
class OverwriteConfirmView(discord.ui.View):
    """Asks before a save replaces an existing template."""

    def __init__(self, state: StudioState, name: str, target: EmbedTemplate):
        super().__init__(timeout=180)
        self.state = state
        self.name = name
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    @discord.ui.button(
        label="Replace it", style=discord.ButtonStyle.danger, emoji="♻️"
    )
    async def replace(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        try:
            result = await state.store.save(
                guild_id=state.guild_id,
                name=self.name,
                script=state.script,
                actor_id=interaction.user.id,
                overwrite=True,
            )
        except TemplateError as exc:
            state.inform(str(exc), "warning")
            await render_studio(interaction, state)
            self.stop()
            return

        state.link_to(result.template)
        await state.reload_templates()
        state.focus(result.template.template_id)
        await state.autosave()
        state.inform(f"Replaced **{result.template.name}**.", "success")
        await render_studio(interaction, state)
        self.stop()

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.state.inform("Nothing was replaced.")
        await render_studio(interaction, self.state)
        self.stop()


class DeleteConfirmView(discord.ui.View):
    """Asks before a template is removed for the whole server."""

    def __init__(self, state: StudioState, template: EmbedTemplate):
        super().__init__(timeout=180)
        self.state = state
        self.template = template

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        try:
            name = await state.store.delete(
                guild_id=state.guild_id, template_id=self.template.template_id
            )
        except TemplateError as exc:
            state.inform(str(exc), "warning")
        else:
            state.inform(f"Deleted **{name}**. The draft itself is untouched.", "success")
            if state.script.template_id == self.template.template_id:
                # The draft stays editable, it just is not linked any more.
                state.unlink()
                await state.autosave()

        if state.selected_id == self.template.template_id:
            state.selected_id = None
        await state.reload_templates()
        await render_manage(interaction, state)
        self.stop()

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.state.inform("Nothing was deleted.")
        await render_manage(interaction, self.state)
        self.stop()


class ClearDraftConfirmView(discord.ui.View):
    def __init__(self, state: StudioState):
        super().__init__(timeout=180)
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    @discord.ui.button(
        label="Start a new draft", style=discord.ButtonStyle.danger, emoji="🧹"
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        state.script = EmbedScript(state.user_id)
        state.saved_payload = None
        try:
            await state.store.clear_draft(
                guild_id=state.guild_id, user_id=state.user_id
            )
        except TemplateError:
            logger.warning("Could not clear the stored draft for %s", state.user_id)
        state.inform("Started a blank draft. Saved templates were kept.", "success")
        await render_studio(interaction, state)
        self.stop()

    @discord.ui.button(label="Keep editing", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.state.inform("Your draft was kept.")
        await render_studio(interaction, self.state)
        self.stop()


# ── manage screen ──────────────────────────────────────────────────────────
class TemplateManageView(discord.ui.View):
    """Rename, delete or load any saved template."""

    def __init__(self, state: StudioState):
        super().__init__(timeout=STUDIO_TIMEOUT)
        self.state = state
        self.add_item(TemplateManageSelect(state, row=0))
        if state.pages > 1:
            self.add_item(PageButton(state, -1, row=1, manage=True))
            self.add_item(PageButton(state, 1, row=1, manage=True))

        has_selection = state.selected() is not None
        self.load_selected.disabled = not has_selection
        self.rename_selected.disabled = not has_selection
        self.delete_selected.disabled = not has_selection

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    def _require_selection(self, state: StudioState) -> EmbedTemplate | None:
        template = state.selected()
        if template is None:
            state.inform("Choose a template in the menu first.", "warning")
        return template

    @discord.ui.button(
        label="Load into studio",
        style=discord.ButtonStyle.primary,
        emoji="📂",
        row=2,
    )
    async def load_selected(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state
        if (chosen := self._require_selection(state)) is None:
            await render_manage(interaction, state)
            return
        await ack(interaction)
        try:
            template = await state.store.get(state.guild_id, chosen.template_id)
        except TemplateError as exc:
            state.inform(str(exc), "warning")
            await state.reload_templates()
            await render_manage(interaction, state)
            return

        state.script = template.to_script(state.user_id)
        state.saved_payload = state.script.to_payload()
        await state.store.mark_used(
            guild_id=state.guild_id, template_id=template.template_id
        )
        await state.autosave()
        state.focus(template.template_id)
        state.inform(f"Loaded **{template.name}** into the studio.", "success")
        await render_studio(interaction, state)
        self.stop()

    @discord.ui.button(
        label="Rename", style=discord.ButtonStyle.secondary, emoji="✏️", row=2
    )
    async def rename_selected(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state
        if (chosen := self._require_selection(state)) is None:
            await render_manage(interaction, state)
            return
        await interaction.response.send_modal(TemplateRenameModal(state, chosen))

    @discord.ui.button(
        label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=2
    )
    async def delete_selected(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state
        if (chosen := self._require_selection(state)) is None:
            await render_manage(interaction, state)
            return
        await _replace(
            interaction,
            embed_builder.warning_embed(
                "Delete this template?",
                "\n".join(
                    [
                        embed_builder.item(f"**{chosen.name}**"),
                        embed_builder.item(f"Used **{chosen.use_count}**× so far"),
                        embed_builder.item(
                            f"Created by {_author(chosen.created_by)} on "
                            f"{_stamp(chosen.created_at)}"
                        ),
                        "",
                        "This removes it for everyone on the server and cannot "
                        "be undone.",
                    ]
                ),
            ),
            DeleteConfirmView(state, chosen),
        )

    @discord.ui.button(
        label="Back to studio", style=discord.ButtonStyle.secondary, emoji="↩️", row=3
    )
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await render_studio(interaction, self.state)
        self.stop()


# ── studio hub ─────────────────────────────────────────────────────────────
class EmbedStudioView(discord.ui.View):
    """The hub: draft controls, the template picker and publishing."""

    def __init__(self, state: StudioState):
        super().__init__(timeout=STUDIO_TIMEOUT)
        self.state = state
        self.add_item(TemplateLoadSelect(state, row=0))
        self.icon_toggle.label = (
            "Server icon: ON" if state.script.show_icon else "Server icon: OFF"
        )
        # A picture always posts without the icon, so the toggle has nothing to do.
        self.icon_toggle.disabled = state.script.has_image
        if state.pages > 1:
            self.add_item(PageButton(state, -1, row=1, manage=False))
            self.add_item(PageButton(state, 1, row=1, manage=False))

        blank = state.script.is_blank
        self.save_template.disabled = blank
        self.update_template.disabled = not (state.linked and state.unsaved_changes)
        self.preview.disabled = blank
        self.clear_draft.disabled = blank
        if state.linked:
            self.update_template.label = _fit(
                f'Update "{state.script.template_name}"', 78
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_manage(interaction)

    # ── row 2: draft and template actions ─────────────────────
    @discord.ui.button(
        label="Edit embed", style=discord.ButtonStyle.primary, emoji="✏️", row=2
    )
    async def edit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state

        async def applied(modal_interaction: discord.Interaction, notes: list[str]) -> None:
            await ack(modal_interaction)
            await state.autosave()
            message = "Draft updated."
            level = "success"
            if notes:
                message = " ".join([message, *notes])
                level = "warning"
            state.inform(message, level)
            await render_studio(modal_interaction, state)

        await interaction.response.send_modal(
            EmbedComposerModal(state.script, on_apply=applied)
        )

    @discord.ui.button(
        label="Banner image", style=discord.ButtonStyle.secondary, emoji="🖼️", row=2
    )
    async def banner_image(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Uploads, replaces or removes the picture inside the embed."""
        state = self.state

        async def applied(
            modal_interaction: discord.Interaction, notice: str, level: str
        ) -> None:
            await state.autosave()
            state.inform(notice, level)
            await render_studio(modal_interaction, state)

        await interaction.response.send_modal(
            BannerModal(state.script, on_apply=applied)
        )

    @discord.ui.button(
        label="Save as template",
        style=discord.ButtonStyle.success,
        emoji="💾",
        row=2,
    )
    async def save_template(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(TemplateNameModal(self.state))

    @discord.ui.button(
        label="Update template",
        style=discord.ButtonStyle.success,
        emoji="🔄",
        row=2,
    )
    async def update_template(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state
        if state.script.template_id is None:
            state.inform("Use **Save as template** first.", "warning")
            await render_studio(interaction, state)
            return
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        try:
            template = await state.store.update(
                guild_id=state.guild_id,
                template_id=state.script.template_id,
                script=state.script,
                actor_id=interaction.user.id,
            )
        except TemplateError as exc:
            state.inform(str(exc), "warning")
            state.unlink()
            await state.reload_templates()
            await render_studio(interaction, state)
            return

        state.link_to(template)
        await state.reload_templates()
        state.focus(template.template_id)
        await state.autosave()
        state.inform(f"**{template.name}** now holds this draft.", "success")
        await render_studio(interaction, state)

    @discord.ui.button(
        label="Manage templates",
        style=discord.ButtonStyle.secondary,
        emoji="🗂️",
        row=2,
    )
    async def manage(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        await state.reload_templates()
        if state.selected_id is None:
            state.selected_id = state.script.template_id
        await render_manage(interaction, state)
        self.stop()

    # ── row 3: publish and housekeeping ───────────────────────
    @discord.ui.button(
        label="Preview & publish",
        style=discord.ButtonStyle.primary,
        emoji="👁️",
        row=3,
    )
    async def preview(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Sent as a separate private message so the studio stays open behind it.
        await show_preview(interaction, self.state.script)

    @discord.ui.button(
        label="Server icon: ON", style=discord.ButtonStyle.secondary, emoji="🏷️", row=3
    )
    async def icon_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Shows or hides the server icon in the embed's top-right corner."""
        await ack(interaction)
        state = self.state
        state.script.show_icon = not state.script.show_icon
        await state.autosave()
        state.inform(
            "The server icon will show on this embed."
            if state.script.show_icon
            else "The server icon is hidden — useful when the message already "
            "has its own banner.",
            "success",
        )
        await render_studio(interaction, state)

    @discord.ui.button(
        label="New draft", style=discord.ButtonStyle.secondary, emoji="🧹", row=3
    )
    async def clear_draft(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.state
        lines = ["This clears the message you are editing."]
        if state.linked:
            lines.append(
                f"**{state.script.template_name}** stays saved in the library."
            )
        await _replace(
            interaction,
            embed_builder.warning_embed("Start a new draft?", "\n".join(lines)),
            ClearDraftConfirmView(state),
        )

    @discord.ui.button(
        label="Close", style=discord.ButtonStyle.secondary, emoji="✖️", row=3
    )
    async def close(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Acknowledge first: the work below waits on SQLite.
        await ack(interaction)
        state = self.state
        await state.autosave()
        note = (
            "Your draft is saved — reopen **Embed Message** to carry on."
            if not state.script.is_blank
            else "Nothing was left unsaved."
        )
        await _replace(
            interaction,
            embed_builder.info_embed("Embed Studio closed", note),
            None,
        )
        self.stop()
