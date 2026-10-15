"""
Weekly SafeRest Discord announcement definitions, their saved templates and their
posting schedules.
Created by Aljay Leodones for SafeRest.

The four recurring posts (Monday kickoff, Wednesday check-in, Thursday hook,
Friday spotlight) are ordinary rows in the Embed Studio template library, so a
moderator can edit them from the dedicated announcement screen *or* from the
studio and both see the same copy. The definitions below only supply the
starting text, the default banner and the default posting time; once a server
has saved its own version, the database always wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import sqlite3
from typing import Any, Optional

from config import COLOR_ACCENT, COLOR_BRAND, COMMUNITY_DATABASE_PATH
from core.database import CommunityDatabase, get_database
from core.embed_script import EmbedScript
from core.embed_templates import EmbedTemplate, TemplateStore, get_template_store

logger = logging.getLogger("saferest_bot.announcements")

# Every seeded template starts with this, so the four weekly posts sort
# together — and in week order — inside the Embed Studio picker.
TEMPLATE_NAME_PREFIX = "SafeRest Announcement"

# guild_settings key linking one announcement to the template row it edits.
SETTING_PREFIX = "announcement_template"

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def setting_key(slug: str) -> str:
    return f"{SETTING_PREFIX}:{slug}"


@dataclass(frozen=True, slots=True)
class Announcement:
    """One recurring weekly post."""

    slug: str
    weekday: int  # 0 = Monday ... 6 = Sunday
    title: str  # "Weekly Kickoff"
    emoji: str
    order: int  # position in the week, used in the template name
    summary: str  # one line shown on the hub
    default_hour: int
    default_minute: int
    payload: dict[str, Any]

    @property
    def day(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    @property
    def label(self) -> str:
        """`Monday Weekly Kickoff` — used on buttons and headings."""
        return f"{self.day} {self.title}"

    @property
    def template_name(self) -> str:
        """`SafeRest Announcement 1 - Monday Weekly Kickoff`"""
        return f"{TEMPLATE_NAME_PREFIX} {self.order} - {self.label}"

    @property
    def default_time(self) -> str:
        return f"{self.default_hour:02d}:{self.default_minute:02d}"


# ── Default copy ────────────────────────────────────────────────────────────
# Discord clients turn `:trophy:` into an emoji as you type; a bot posting the
# same text through the API would publish the literal colon form, so every
# shortcode from the source document is stored here as the character itself.

# No SafeRest kickoff banner yet. Upload one from the announcement screen, or
# paste its link here.
MONDAY_BANNER = None

# TODO: confirm both SafeRest links in Euka before posting these announcements.
CONTEST_URL = "https://creators.euka.ai/contests/saferest-weekly-posting-challenge"
CONNECT_URL = "https://creators.euka.ai/discord/saferest/join"

MONDAY_BODY = f"""## Hi everyone! We are so excited to announce that we are launching a Weekly Posting Challenge starting TODAY and the timing could not be more perfect!

**Here's how it works**: enroll in the challenge, keep posting your TikTok Shop videos linking our SafeRest products like you normally would, and watch your ranking climb on the leaderboard in real time. The top 3 creators at the end of each week take home cash prizes and then the leaderboard resets, giving everyone a fresh shot at the top spot every single week!

> 🥇 *1st Place*: $200
> 🥈 *2nd Place*: $100
> 🥉 *3rd Place*: $50

**In the event of a tie in video count, the creator with the higher GMV for the week will be selected as the winner.**

And here's why this week is the BEST week to start posting: tomorrow kicks off our Brand Spotlight Week where TikTok will be pushing increased traffic and visibility to all SafeRest videos AND we are ramping up our own ad spend on top of that.

Your videos are going to be seen by more people than ever, making this the most powerful week to post that we've ever had! 🚀✨

**To enter, click the link below!** One important note: you must have your TikTok account connected to your Discord to register.

- If you haven't linked them yet you can do so here first and then give it about an hour to process before registering. Once it's connected, register for the challenge and you're in!

- If you run into any issues just let me know and I'll get you sorted.

Connect your TikTok to Discord [here]({CONNECT_URL}) 👇
{CONNECT_URL}

Register for the Weekly Posting Challenge [here]({CONTEST_URL}) 👇
{CONTEST_URL}

## Let's make this our biggest week yet! 🛍️"""

WEDNESDAY_BODY = f"""## We are halfway through the week — here is where things stand!

The leaderboard is live and moving, and there is still plenty of time to climb before it resets. If you have been posting, keep the momentum going. If you have not started yet, **today is the day** — a few strong videos can still put you in the top 3.

> 🥇 *1st Place*: $200
> 🥈 *2nd Place*: $100
> 🥉 *3rd Place*: $50

**Three reminders so every video counts:**
- Every video has to link a SafeRest product through TikTok Shop.
- Your TikTok account must stay connected to Discord — that connection is how your posts get tracked.
- Ties are broken by GMV for the week, so a video that actually converts still beats a video that only posts.

**Not sure what to film today?** Spill tests, bedroom refresh makeovers and honest first-impression reviews are the formats performing best for SafeRest right now.

Check the live leaderboard [here]({CONTEST_URL}) 👇
{CONTEST_URL}

Questions? Drop them in the chat — we are around all day. 💬"""

THURSDAY_BODY = """## Steal this hook — it is working right now.

The first two seconds decide whether your video gets watched or scrolled past. Here is the opener outperforming everything else for SafeRest creators this week:

> ✨ *"I didn't believe a waterproof mattress protector could feel this soft — until I poured a whole glass of water on it."*

**Why it works**
- It opens on a doubt the viewer already has, so they stay to see it resolved.
- "A whole glass of water" is specific and checkable, and specificity reads as honest.
- The payoff is visual, so the rest of the video writes itself.

**How to use it today**
- Say the hook on camera in the first 2 seconds — no logo intro, no "hey guys".
- Get the product on screen within 3 seconds of the hook landing.
- Add the product to your TikTok Shop showcase *before* you post, not after.

**Film it today and drop your link in the chat** — the strongest one gets featured in tomorrow's Creator Spotlight. 🔥"""

FRIDAY_BODY = f"""## This week's spotlight goes to...

Every Friday we highlight one creator whose work moved the needle for SafeRest — not always the biggest account, but the one who nailed the execution.

> 🏆 **Creator:** @handle
> 📈 **Videos this week:** 0
> 💰 **GMV driven:** $0

**What made it work**
- (what the hook did)
- (what the edit did)
- (what the call to action did)

**Watch it here:** (paste the video link)

Congratulations 🎉 — and for everyone else: the leaderboard resets this weekend, so this could be your slot next week.

See the full standings [here]({CONTEST_URL}) 👇
{CONTEST_URL}"""


ANNOUNCEMENTS: tuple[Announcement, ...] = (
    Announcement(
        slug="monday_kickoff",
        weekday=0,
        title="Weekly Kickoff",
        emoji="🏆",
        order=1,
        summary="Launches the week and opens the posting challenge.",
        default_hour=9,
        default_minute=0,
        payload={
            "content": "@everyone",
            "title": "🏆 Introducing Our Weekly Posting Challenge!",
            "description": MONDAY_BODY,
            "image_url": MONDAY_BANNER,
            "color": COLOR_ACCENT,
            "show_icon": True,
        },
    ),
    Announcement(
        slug="wednesday_checkin",
        weekday=2,
        title="Mid-Week Check-In",
        emoji="📊",
        order=2,
        summary="Nudges the leaderboard while there is still time to climb.",
        default_hour=12,
        default_minute=0,
        payload={
            "content": None,
            "title": "📊 Mid-Week Check-In — how is the leaderboard looking?",
            "description": WEDNESDAY_BODY,
            "image_url": None,
            "color": COLOR_BRAND,
        },
    ),
    Announcement(
        slug="thursday_hook",
        weekday=3,
        title="Hook of the Week",
        emoji="🎣",
        order=3,
        summary="One proven opening line creators can copy the same day.",
        default_hour=10,
        default_minute=0,
        payload={
            "content": None,
            "title": "🎣 Hook of the Week",
            "description": THURSDAY_BODY,
            "image_url": None,
            "color": COLOR_BRAND,
        },
    ),
    Announcement(
        slug="friday_spotlight",
        weekday=4,
        title="Creator Spotlight",
        emoji="🌟",
        order=4,
        summary="Celebrates one creator and closes out the week.",
        default_hour=15,
        default_minute=0,
        payload={
            "content": None,
            "title": "🌟 Creator Spotlight",
            "description": FRIDAY_BODY,
            "image_url": None,
            "color": COLOR_ACCENT,
        },
    ),
)

BY_SLUG: dict[str, Announcement] = {item.slug: item for item in ANNOUNCEMENTS}


def get_announcement(slug: str) -> Announcement:
    try:
        return BY_SLUG[slug]
    except KeyError:
        raise KeyError(f"Unknown announcement: {slug}") from None


# ── Schedules ───────────────────────────────────────────────────────────────
@dataclass(slots=True)
class AnnouncementSchedule:
    """When and where one announcement posts itself."""

    guild_id: int
    slug: str
    channel_id: int
    weekday: int
    hour: int
    minute: int
    mention_everyone: bool
    enabled: bool
    last_run: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AnnouncementSchedule":
        return cls(
            guild_id=row["guild_id"],
            slug=row["slug"],
            channel_id=row["channel_id"],
            weekday=row["weekday"],
            hour=row["hour"],
            minute=row["minute"],
            mention_everyone=bool(row["mention_everyone"]),
            enabled=bool(row["enabled"]),
            last_run=row["last_run"],
        )

    @property
    def clock(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def day(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    def describe(self) -> str:
        state = "" if self.enabled else "  *(paused)*"
        ping = "  ·  pings @everyone" if self.mention_everyone else ""
        return (
            f"Every **{self.day} {self.clock}** in <#{self.channel_id}>"
            f"{ping}{state}"
        )


def parse_time(raw: Any) -> tuple[int, int]:
    """Reads `9`, `9:30`, `09:30` or `21:05` into (hour, minute)."""
    text = str(raw or "").strip().replace(".", ":")
    if not text:
        raise ValueError("Enter a time such as `09:00`.")
    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text) if minute_text else 0
    except ValueError:
        raise ValueError(
            f"`{text}` is not a time. Use 24-hour format, for example `09:00` "
            "or `15:30`."
        ) from None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(
            f"`{text}` is outside a real day. Use 24-hour format between "
            "`00:00` and `23:59`."
        )
    return hour, minute


def parse_weekday(raw: Any, *, default: int) -> int:
    """Accepts `Mon`, `monday`, `0` or a blank (which keeps `default`)."""
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text.isdigit():
        value = int(text)
        if 0 <= value <= 6:
            return value
        raise ValueError("Day numbers run from `0` (Monday) to `6` (Sunday).")
    for index, name in enumerate(WEEKDAY_NAMES):
        if name.lower().startswith(text):
            return index
    raise ValueError(f"`{raw}` is not a day. Use a name such as `Monday` or `Wed`.")


class AnnouncementStore:
    """Links each weekly post to its saved template and posting schedule."""

    def __init__(
        self,
        database: CommunityDatabase | None = None,
        templates: TemplateStore | None = None,
    ):
        self._db = database or get_database(COMMUNITY_DATABASE_PATH)
        self._templates = templates or get_template_store()

    @property
    def templates(self) -> TemplateStore:
        return self._templates

    async def _ready(self) -> None:
        await self._db.initialize()

    # ── template linking ──────────────────────────────────────
    async def resolve(
        self, guild_id: int, announcement: Announcement, *, actor_id: int = 0
    ) -> EmbedTemplate:
        """
        Returns the template row this announcement edits, creating it on the
        first run.

        The link is stored by id, so renaming the template in the Embed Studio
        keeps the announcement button pointed at it; the name lookup is only
        the fallback for a link that was never written or that points at a row
        someone deleted.
        """
        await self._ready()
        key = setting_key(announcement.slug)
        stored = await self._db.get_setting(guild_id, key)
        if stored and stored.isdigit():
            template = await self._templates.find(guild_id, int(stored))
            if template is not None:
                return template

        template = await self._templates.find_by_name(
            guild_id, announcement.template_name
        )
        if template is None:
            result = await self._templates.save(
                guild_id=guild_id,
                name=announcement.template_name,
                script=EmbedScript.from_payload(announcement.payload, user_id=actor_id),
                actor_id=actor_id,
                overwrite=True,
            )
            template = result.template
            logger.info(
                "Seeded announcement template %r for guild %s",
                template.name,
                guild_id,
            )
        await self._db.set_setting(guild_id, key, str(template.template_id))
        return template

    async def seed_all(self, guild_id: int, *, actor_id: int = 0) -> None:
        """Makes sure all four templates exist for a server."""
        for announcement in ANNOUNCEMENTS:
            await self.resolve(guild_id, announcement, actor_id=actor_id)

    async def script(
        self, guild_id: int, announcement: Announcement, *, user_id: int = 0
    ) -> EmbedScript:
        """The current copy, as an editable draft."""
        template = await self.resolve(guild_id, announcement, actor_id=user_id)
        if template.damaged:
            # One unreadable row must not take the announcement offline: fall
            # back to the shipped copy, still linked so a save repairs it.
            script = EmbedScript.from_payload(announcement.payload, user_id=user_id)
            script.template_id = template.template_id
            script.template_name = template.name
            return script
        return template.to_script(user_id)

    async def save(
        self,
        *,
        guild_id: int,
        announcement: Announcement,
        script: EmbedScript,
        actor_id: int,
    ) -> EmbedTemplate:
        """Writes edited copy back to the announcement's template."""
        template = await self.resolve(guild_id, announcement, actor_id=actor_id)
        return await self._templates.update(
            guild_id=guild_id,
            template_id=template.template_id,
            script=script,
            actor_id=actor_id,
        )

    async def reset(
        self, *, guild_id: int, announcement: Announcement, actor_id: int
    ) -> EmbedTemplate:
        """Restores the shipped copy, banner and colour."""
        return await self.save(
            guild_id=guild_id,
            announcement=announcement,
            script=EmbedScript.from_payload(announcement.payload, user_id=actor_id),
            actor_id=actor_id,
        )

    # ── schedules ─────────────────────────────────────────────
    async def get_schedule(
        self, guild_id: int, slug: str
    ) -> AnnouncementSchedule | None:
        await self._ready()
        row = await self._db.fetchone(
            "SELECT * FROM announcement_schedules WHERE guild_id = ? AND slug = ?",
            (guild_id, slug),
        )
        return AnnouncementSchedule.from_row(row) if row else None

    async def list_schedules(self, guild_id: int) -> dict[str, AnnouncementSchedule]:
        await self._ready()
        rows = await self._db.fetchall(
            "SELECT * FROM announcement_schedules WHERE guild_id = ?", (guild_id,)
        )
        return {row["slug"]: AnnouncementSchedule.from_row(row) for row in rows}

    async def set_schedule(
        self,
        *,
        guild_id: int,
        slug: str,
        channel_id: int,
        weekday: int,
        hour: int,
        minute: int,
        mention_everyone: bool,
        enabled: bool = True,
        actor_id: int = 0,
    ) -> None:
        await self._ready()
        await self._db.execute(
            """
            INSERT INTO announcement_schedules
                (guild_id, slug, channel_id, weekday, hour, minute,
                 mention_everyone, enabled, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id, slug) DO UPDATE SET
                channel_id       = excluded.channel_id,
                weekday          = excluded.weekday,
                hour             = excluded.hour,
                minute           = excluded.minute,
                mention_everyone = excluded.mention_everyone,
                enabled          = excluded.enabled,
                updated_by       = excluded.updated_by,
                updated_at       = datetime('now')
            """,
            (
                guild_id,
                slug,
                channel_id,
                weekday,
                hour,
                minute,
                int(mention_everyone),
                int(enabled),
                actor_id,
            ),
        )

    async def set_enabled(self, *, guild_id: int, slug: str, enabled: bool) -> bool:
        await self._ready()
        result = await self._db.execute_write(
            """
            UPDATE announcement_schedules
            SET enabled = ?, updated_at = datetime('now')
            WHERE guild_id = ? AND slug = ?
            """,
            (int(enabled), guild_id, slug),
        )
        return bool(result.rowcount)

    async def clear_schedule(self, *, guild_id: int, slug: str) -> bool:
        await self._ready()
        result = await self._db.execute_write(
            "DELETE FROM announcement_schedules WHERE guild_id = ? AND slug = ?",
            (guild_id, slug),
        )
        return bool(result.rowcount)

    async def mark_run(self, *, guild_id: int, slug: str, day: str) -> None:
        await self._ready()
        await self._db.execute(
            "UPDATE announcement_schedules SET last_run = ? "
            "WHERE guild_id = ? AND slug = ?",
            (day, guild_id, slug),
        )

    async def due(self, now: datetime) -> list[AnnouncementSchedule]:
        """
        Every enabled schedule whose slot has passed today and has not fired.

        Comparing against `last_run` rather than the exact minute means a bot
        that was restarting at 09:00 still publishes the Monday post when it
        comes back at 09:04 — but only once, and never on the following day.
        """
        await self._ready()
        rows = await self._db.fetchall(
            "SELECT * FROM announcement_schedules WHERE enabled = 1"
        )
        today = now.date().isoformat()
        pending: list[AnnouncementSchedule] = []
        for row in rows:
            schedule = AnnouncementSchedule.from_row(row)
            if schedule.slug not in BY_SLUG:
                continue
            if schedule.weekday != now.weekday():
                continue
            if (now.hour, now.minute) < (schedule.hour, schedule.minute):
                continue
            if schedule.last_run == today:
                continue
            pending.append(schedule)
        return pending


_shared_store: AnnouncementStore | None = None


def get_announcement_store() -> AnnouncementStore:
    """Process-wide store, sharing the community database connection."""
    global _shared_store
    if _shared_store is None:
        _shared_store = AnnouncementStore()
    return _shared_store


__all__ = [
    "ANNOUNCEMENTS",
    "Announcement",
    "AnnouncementSchedule",
    "AnnouncementStore",
    "WEEKDAY_NAMES",
    "get_announcement",
    "get_announcement_store",
    "parse_time",
    "parse_weekday",
]
