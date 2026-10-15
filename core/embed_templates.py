"""
Saved embed template library and auto-saved composer drafts.
Created by Aljay Leodones for SafeRest.

Templates are stored per server in the shared community database, so anything
a moderator saves survives restarts and is available to the whole team. Every
public method here either succeeds or raises `TemplateError` carrying a message
that can be shown to the moderator as-is — the Embed Studio never has to guess
what went wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any, NamedTuple, Optional

from config import COMMUNITY_DATABASE_PATH
from core.database import CommunityDatabase, get_database
from core.embed_script import EmbedScript

logger = logging.getLogger("saferest_bot.embed_templates")

# Keeping a ceiling here means the picker always stays browsable and one
# runaway loop can never fill the database.
MAX_TEMPLATES_PER_GUILD = 100
MAX_NAME_LENGTH = 60

STORAGE_ERROR = (
    "The template library could not be reached. The bot console has the "
    "details — try again in a moment."
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


class TemplateError(RuntimeError):
    """Raised with a message that is safe to show a moderator verbatim."""


class DuplicateTemplateError(TemplateError):
    """A template with that name already exists; the caller may overwrite it."""

    def __init__(self, template: "EmbedTemplate"):
        super().__init__(
            f"A template named **{template.name}** already exists."
        )
        self.template = template


def normalize_name(raw: Any) -> str:
    """
    Cleans a typed template name into its stored form.

    Control characters are dropped and runs of whitespace collapse to single
    spaces, so `"  Weekly \n restock "` and `"Weekly restock"` are the same
    name rather than two near-identical entries in the picker.
    """
    cleaned = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub("", str(raw or ""))).strip()
    if not cleaned:
        raise TemplateError(
            "Give the template a name — for example `Weekly restock`."
        )
    if len(cleaned) > MAX_NAME_LENGTH:
        raise TemplateError(
            f"Template names can be at most {MAX_NAME_LENGTH} characters "
            f"(that one is {len(cleaned)})."
        )
    return cleaned


def name_key(name: str) -> str:
    """Casefolded key, so `Restock` and `restock` are one template."""
    return name.casefold()


@dataclass(slots=True)
class EmbedTemplate:
    """One stored template row."""

    template_id: int
    guild_id: int
    name: str
    payload: dict[str, Any]
    damaged: bool = False
    created_by: Optional[int] = None
    created_at: str = ""
    updated_by: Optional[int] = None
    updated_at: str = ""
    use_count: int = 0
    last_used_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "EmbedTemplate":
        """
        Builds a template from a database row.

        A row whose JSON cannot be read is returned as `damaged` instead of
        raising, so one bad entry never hides the rest of the library.
        """
        payload: dict[str, Any] = {}
        damaged = False
        try:
            decoded = json.loads(row["payload"])
            if isinstance(decoded, dict):
                payload = decoded
            else:
                damaged = True
        except (TypeError, ValueError):
            damaged = True
            logger.warning(
                "Template %s in guild %s has unreadable payload data",
                row["template_id"],
                row["guild_id"],
            )

        return cls(
            template_id=row["template_id"],
            guild_id=row["guild_id"],
            name=row["name"],
            payload=payload,
            damaged=damaged,
            created_by=row["created_by"],
            created_at=row["created_at"] or "",
            updated_by=row["updated_by"],
            updated_at=row["updated_at"] or "",
            use_count=row["use_count"] or 0,
            last_used_at=row["last_used_at"],
        )

    def to_script(self, user_id: int) -> EmbedScript:
        """Hydrates the template into an editable draft."""
        script = EmbedScript.from_payload(self.payload, user_id=user_id)
        script.template_id = self.template_id
        script.template_name = self.name
        return script

    def preview_line(self, limit: int = 90) -> str:
        """One-line summary used as the picker option description."""
        if self.damaged:
            return "Saved data is damaged — re-save this template"
        source = (
            self.payload.get("title")
            or self.payload.get("description")
            or self.payload.get("content")
            or ""
        )
        summary = _WHITESPACE.sub(" ", str(source)).strip()
        if not summary:
            return "Image-only template"
        return summary[: limit - 1] + "…" if len(summary) > limit else summary


class SaveResult(NamedTuple):
    template: EmbedTemplate
    created: bool


class TemplateStore:
    """Async CRUD over `embed_templates` and `embed_drafts`."""

    def __init__(self, database: CommunityDatabase | None = None):
        self._db = database or get_database(COMMUNITY_DATABASE_PATH)

    # ── plumbing ──────────────────────────────────────────────
    async def _ready(self) -> None:
        """
        Guarantees the connection and schema exist.

        `initialize()` is idempotent, so calling it before each operation
        costs nothing and means the studio works even if it is opened before
        a cog has finished loading.
        """
        try:
            await self._db.initialize()
        except sqlite3.Error as exc:
            logger.exception("Could not open the template library")
            raise TemplateError(STORAGE_ERROR) from exc

    async def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        await self._ready()
        try:
            return await self._db.fetchall(sql, params)
        except (sqlite3.Error, RuntimeError) as exc:
            logger.exception("Template read failed")
            raise TemplateError(STORAGE_ERROR) from exc

    async def _fetchone(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        await self._ready()
        try:
            return await self._db.fetchone(sql, params)
        except (sqlite3.Error, RuntimeError) as exc:
            logger.exception("Template read failed")
            raise TemplateError(STORAGE_ERROR) from exc

    async def _write(self, sql: str, params: tuple[Any, ...]):
        await self._ready()
        try:
            return await self._db.execute_write(sql, params)
        except sqlite3.IntegrityError:
            # The unique (guild_id, name_key) index fired — the caller knows
            # what that means for the operation it attempted.
            raise
        except (sqlite3.Error, RuntimeError) as exc:
            logger.exception("Template write failed")
            raise TemplateError(STORAGE_ERROR) from exc

    @staticmethod
    def _encode(script: EmbedScript) -> str:
        if script.is_blank:
            raise TemplateError(
                "There is nothing to save yet — write the message first."
            )
        return json.dumps(script.to_payload(), ensure_ascii=False)

    # ── reads ─────────────────────────────────────────────────
    async def count(self, guild_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS total FROM embed_templates WHERE guild_id = ?",
            (guild_id,),
        )
        return row["total"] if row else 0

    async def list_templates(self, guild_id: int) -> list[EmbedTemplate]:
        """Every template for the server, ordered alphabetically."""
        rows = await self._fetchall(
            """
            SELECT * FROM embed_templates
            WHERE guild_id = ?
            ORDER BY name_key
            LIMIT ?
            """,
            (guild_id, MAX_TEMPLATES_PER_GUILD),
        )
        return [EmbedTemplate.from_row(row) for row in rows]

    async def find(self, guild_id: int, template_id: int) -> EmbedTemplate | None:
        row = await self._fetchone(
            "SELECT * FROM embed_templates WHERE guild_id = ? AND template_id = ?",
            (guild_id, template_id),
        )
        return EmbedTemplate.from_row(row) if row else None

    async def get(self, guild_id: int, template_id: int) -> EmbedTemplate:
        """Fetches one template, or explains why it cannot be used."""
        template = await self.find(guild_id, template_id)
        if template is None:
            raise TemplateError(
                "That template no longer exists — someone may have deleted it. "
                "Reopen the list to see what is saved now."
            )
        if template.damaged:
            raise TemplateError(
                f"**{template.name}** could not be read back. Compose the "
                "message again and save it over this template to repair it."
            )
        return template

    async def find_by_name(self, guild_id: int, name: str) -> EmbedTemplate | None:
        row = await self._fetchone(
            "SELECT * FROM embed_templates WHERE guild_id = ? AND name_key = ?",
            (guild_id, name_key(name)),
        )
        return EmbedTemplate.from_row(row) if row else None

    # ── writes ────────────────────────────────────────────────
    async def save(
        self,
        *,
        guild_id: int,
        name: Any,
        script: EmbedScript,
        actor_id: int,
        overwrite: bool = False,
    ) -> SaveResult:
        """
        Saves the draft under `name`.

        Raises `DuplicateTemplateError` when the name is taken and `overwrite`
        was not granted, so the studio can ask before replacing someone's work.
        """
        clean_name = normalize_name(name)
        payload = self._encode(script)

        existing = await self.find_by_name(guild_id, clean_name)
        if existing is not None:
            if not overwrite:
                raise DuplicateTemplateError(existing)
            await self._write(
                """
                UPDATE embed_templates
                SET name = ?, payload = ?, updated_by = ?,
                    updated_at = datetime('now')
                WHERE guild_id = ? AND template_id = ?
                """,
                (clean_name, payload, actor_id, guild_id, existing.template_id),
            )
            return SaveResult(await self.get(guild_id, existing.template_id), False)

        if await self.count(guild_id) >= MAX_TEMPLATES_PER_GUILD:
            raise TemplateError(
                f"This server already has the maximum of "
                f"{MAX_TEMPLATES_PER_GUILD} templates. Delete one you no "
                "longer use, then save again."
            )

        try:
            result = await self._write(
                """
                INSERT INTO embed_templates
                    (guild_id, name, name_key, payload, created_by, updated_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    clean_name,
                    name_key(clean_name),
                    payload,
                    actor_id,
                    actor_id,
                ),
            )
        except sqlite3.IntegrityError:
            # Two moderators saved the same name at the same instant.
            duplicate = await self.find_by_name(guild_id, clean_name)
            if duplicate is None:
                raise TemplateError(STORAGE_ERROR) from None
            raise DuplicateTemplateError(duplicate) from None

        template_id = result.lastrowid
        if template_id is None:  # pragma: no cover - SQLite always reports one
            raise TemplateError(STORAGE_ERROR)
        return SaveResult(await self.get(guild_id, template_id), True)

    async def update(
        self, *, guild_id: int, template_id: int, script: EmbedScript, actor_id: int
    ) -> EmbedTemplate:
        """Overwrites the contents of a known template, keeping its name."""
        payload = self._encode(script)
        result = await self._write(
            """
            UPDATE embed_templates
            SET payload = ?, updated_by = ?, updated_at = datetime('now')
            WHERE guild_id = ? AND template_id = ?
            """,
            (payload, actor_id, guild_id, template_id),
        )
        if not result.rowcount:
            raise TemplateError(
                "That template no longer exists, so there was nothing to "
                "update. Use **Save as template** to store this draft instead."
            )
        return await self.get(guild_id, template_id)

    async def rename(
        self, *, guild_id: int, template_id: int, name: Any, actor_id: int
    ) -> EmbedTemplate:
        clean_name = normalize_name(name)
        try:
            result = await self._write(
                """
                UPDATE embed_templates
                SET name = ?, name_key = ?, updated_by = ?,
                    updated_at = datetime('now')
                WHERE guild_id = ? AND template_id = ?
                """,
                (
                    clean_name,
                    name_key(clean_name),
                    actor_id,
                    guild_id,
                    template_id,
                ),
            )
        except sqlite3.IntegrityError:
            existing = await self.find_by_name(guild_id, clean_name)
            if existing is not None and existing.template_id != template_id:
                raise DuplicateTemplateError(existing) from None
            raise TemplateError(STORAGE_ERROR) from None

        if not result.rowcount:
            raise TemplateError("That template no longer exists.")
        return await self.get(guild_id, template_id)

    async def delete(self, *, guild_id: int, template_id: int) -> str:
        """Deletes a template and returns the name it had."""
        template = await self.find(guild_id, template_id)
        if template is None:
            raise TemplateError("That template has already been deleted.")
        result = await self._write(
            "DELETE FROM embed_templates WHERE guild_id = ? AND template_id = ?",
            (guild_id, template_id),
        )
        if not result.rowcount:
            raise TemplateError("That template has already been deleted.")
        logger.info(
            "Deleted embed template %s (%s) in guild %s",
            template_id,
            template.name,
            guild_id,
        )
        return template.name

    async def mark_used(self, *, guild_id: int, template_id: int) -> None:
        """Bumps the usage counter. Never fails the caller."""
        try:
            await self._write(
                """
                UPDATE embed_templates
                SET use_count = use_count + 1, last_used_at = datetime('now')
                WHERE guild_id = ? AND template_id = ?
                """,
                (guild_id, template_id),
            )
        except (TemplateError, sqlite3.Error):
            logger.warning(
                "Could not record usage for template %s in guild %s",
                template_id,
                guild_id,
            )

    # ── auto-saved drafts ─────────────────────────────────────
    async def save_draft(
        self, *, guild_id: int, user_id: int, script: EmbedScript
    ) -> None:
        """
        Stores the moderator's in-progress draft.

        The studio calls this after every change, so a closed window, an
        expired panel or a bot restart never loses unsaved work.
        """
        if script.is_blank:
            await self.clear_draft(guild_id=guild_id, user_id=user_id)
            return
        await self._write(
            """
            INSERT INTO embed_drafts (guild_id, user_id, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = datetime('now')
            """,
            (
                guild_id,
                user_id,
                json.dumps(script.to_draft_payload(), ensure_ascii=False),
            ),
        )

    async def load_draft(self, *, guild_id: int, user_id: int) -> EmbedScript | None:
        """Restores the stored draft, or `None` when there is nothing usable."""
        row = await self._fetchone(
            "SELECT payload FROM embed_drafts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            logger.warning("Discarding unreadable draft for user %s", user_id)
            await self.clear_draft(guild_id=guild_id, user_id=user_id)
            return None
        if not isinstance(payload, dict):
            await self.clear_draft(guild_id=guild_id, user_id=user_id)
            return None

        script = EmbedScript(user_id).apply_draft_payload(payload)
        return None if script.is_blank else script

    async def clear_draft(self, *, guild_id: int, user_id: int) -> None:
        await self._write(
            "DELETE FROM embed_drafts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )


_shared_store: TemplateStore | None = None


def get_template_store(database_path: Path | None = None) -> TemplateStore:
    """Process-wide template store, sharing the community database connection."""
    global _shared_store
    if _shared_store is None:
        _shared_store = TemplateStore(
            get_database(database_path or COMMUNITY_DATABASE_PATH)
        )
    return _shared_store


__all__ = [
    "DuplicateTemplateError",
    "EmbedTemplate",
    "MAX_NAME_LENGTH",
    "MAX_TEMPLATES_PER_GUILD",
    "SaveResult",
    "TemplateError",
    "TemplateStore",
    "get_template_store",
    "normalize_name",
]
