# True Classic Bot — Onboarding, Custom Reporting & Formatting Guide
**Author:** Aljay Leodones  
**Organization:** True Classic  
**Details:** Prepared for True Classic - The features of this Bot are original and can't be found in any other 3rd-party bots like Mee6, Dyno, etc  

This document provides a comprehensive, detailed technical specification of how the **Onboarding System**, **Custom Reporting Engine**, and **Formatting / Embed Architecture** were built in the True Classic Discord Bot. It is structured so that any engineer or developer can duplicate or re-implement these systems easily.

---

## Architecture Overview

```
                        ┌─────────────────────────────────────────────────────────┐
                        │                True Classic Discord Bot                 │
                        └───────────────────────────┬─────────────────────────────┘
                                                    │
        ┌───────────────────────────────────────────┼───────────────────────────────────────────┐
        ▼                                           ▼                                           ▼
┌───────────────────────────────┐       ┌───────────────────────────────┐       ┌───────────────────────────────┐
│     1. Onboarding Module      │       │   2. Custom Reporting Module   │       │   3. Formatting & Embed Engine│
│   (cogs/onboarding.py)        │       │    (cogs/memberreport.py)     │       │ (utils/embed_builder.py,      │
├───────────────────────────────┤       ├───────────────────────────────┤ │  core/embed_script.py)        │
│ • Member scanning (0-30 days) │       │ • Daily snapshots & baselines │ ├───────────────────────────────┤
│ • Group select UI & modals    │       │ • Downtime-proof join reconc. │ │ • Unified brand design system │
│ • Channel welcome + DM engine │       │ • Growth deltas & trend lists │ │ • Placeholder hydration       │
│ • Database onboarding log     │       │ • Live multi-report refresher │ │ • Multi-embed state (up to 10)│
└───────────────────────────────┘       └───────────────────────────────┘ └───────────────────────────────┘
```

---

## 1. Database Schema Specifications

All three systems share SQLite tables managed asynchronously via `aiosqlite`.

```sql
-- ── 1. Onboarding Tracking ──────────────────────────────
CREATE TABLE IF NOT EXISTS onboarded_members (
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    onboarded_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, guild_id)
);

-- ── 2. Member Snapshots (Daily Growth Baselines) ─────────
CREATE TABLE IF NOT EXISTS member_snapshots (
    guild_id      INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,   -- 'YYYY-MM-DD' (UTC)
    total         INTEGER NOT NULL,
    humans        INTEGER NOT NULL,
    bots          INTEGER NOT NULL,
    online        INTEGER NOT NULL DEFAULT 0,
    admins        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, snapshot_date)
);

-- ── 3. Join Tally (Daily and Historical Seeds) ───────────
CREATE TABLE IF NOT EXISTS member_joins (
    guild_id    INTEGER NOT NULL,
    period      TEXT NOT NULL,    -- 'day' or 'week'
    period_date TEXT NOT NULL,    -- 'YYYY-MM-DD' (UTC; week = Monday)
    joins       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, period, period_date)
);

-- ── 4. Deployed Public Reports Config ────────────────────
CREATE TABLE IF NOT EXISTS member_reports (
    guild_id    INTEGER NOT NULL,
    timeframe   TEXT NOT NULL,    -- 'daily' / 'weekly' / 'monthly'
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, timeframe)
);

-- ── 5. Live Daily Engagement Rollup ──────────────────────
CREATE TABLE IF NOT EXISTS engagement_daily (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,  -- Tracked root channel
    user_id    INTEGER NOT NULL,
    day        TEXT NOT NULL,     -- 'YYYY-MM-DD' (UTC)
    messages   INTEGER NOT NULL DEFAULT 0,
    reactions  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id, user_id, day)
);

-- ── 6. Embed Editor Sessions ─────────────────────────────
CREATE TABLE IF NOT EXISTS editor_sessions (
    message_id   INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    session_type TEXT NOT NULL,  -- 'embed' or 'hook'
    payload      TEXT NOT NULL,   -- JSON EmbedScript state
    updated_at   TEXT DEFAULT (datetime('now'))
);
```

---

## 2. Onboarding Messages System (Bot Channel Post + Warm DM)

### Objective
Allow moderators to scan the server for un-onboarded members who joined in the last 30 days, group them by join date, compose a customizable welcome post, publish it to a selected public channel (tagging all target members), send a warm DM to each member with direct deep-links to onboarding hubs, and mark them as onboarded in SQLite.

### Workflow & Component Chain

```
[Moderator clicks "Scan for New Members"]
                 │
                 ▼
[OnboardingHubView.btn_scan()] ──► Queries DB (onboarded_members) & guild.members (joined within 30 days)
                 │
                 ▼
[OnboardingGroupSelect] ──────────► Displays dropdown grouped by join age ("Joined Today", "Joined 1 day ago")
                 │
                 ▼
[OnboardingComposerModal] ────────► Opens Modal dialog pre-filled with DEFAULT_MESSAGE template
                 │
                 ▼
[OnboardingDeliveryView] ─────────► Offers options: "Post Publicly + DM Welcome" or "DM Welcome Only"
                 │
                 ▼
[OnboardingChannelSelectView] ────► Displays ChannelSelect menu to pick target text channel
                 │
                 ├─► 1. Posts tagged public message to channel: "<@user1> <@user2> ... \n\n {content}"
                 ├─► 2. Runs _dm_welcome(): Sends branded DM embed with channel deep-links
                 └─► 3. Runs _mark_onboarded(): Inserts user IDs into onboarded_members table
```

### Core Implementation Details

#### 1. Default Message Template & Channel Mentions
```python
ONBOARDING_CHANNEL_MENTION = f"<#{config.ONBOARDING_CHANNEL_ID}>"
INTRODUCTIONS_CHANNEL_MENTION = f"<#{config.INTRODUCTIONS_CHANNEL_ID}>"

DEFAULT_MESSAGE = (
    "👋 **Please give a warm welcome to our newest members!**\n\n"
    "Welcome to the **True Classic** affiliate community — we're thrilled to have you here! 🎉\n\n"
    f"**◈  Start here →** {ONBOARDING_CHANNEL_MENTION}\n"
    "Everything you need to hit the ground running lives there — guides, key resources, "
    "and all the essentials.\n\n"
    f"**◈  Say hello →** {INTRODUCTIONS_CHANNEL_MENTION}\n"
    "Pop in to introduce yourself! Tell us a little about you, drop your social media "
    "handles, and share your @s so the community can connect with you. 💚"
)
```

#### 2. Warm DM Embed Builder (`_build_welcome_dm`)
Uses direct `discord.com/channels/{guild_id}/{channel_id}` deep-links so the buttons/links work seamlessly even from a Direct Message context:
```python
def _build_welcome_dm(member: discord.Member) -> discord.Embed:
    guild = member.guild
    onboarding_url = f"https://discord.com/channels/{guild.id}/{config.ONBOARDING_CHANNEL_ID}"
    intros_url = f"https://discord.com/channels/{guild.id}/{config.INTRODUCTIONS_CHANNEL_ID}"

    embed = embed_builder.base_embed(
        title="👋  Welcome to True Classic!",
        description=(
            f"Hi {member.mention}, we're so glad you're here! 🎉\n\n"
            f"You've just joined the **{guild.name}** affiliate community, and we want "
            "to make sure you have the smoothest possible start.\n\n"
            f"**◈  Start here →** [Open the #onboarding channel]({onboarding_url})\n"
            "That's your one-stop hub — guides, key resources, and answers to the most "
            "common questions all live there.\n\n"
            f"**◈  Introduce yourself →** [Head to #introductions]({intros_url})\n"
            "Don't be shy — say hello, tell us a bit about yourself, and feel free to "
            "share your social media handles and @s so the community can connect with you.\n\n"
            "Our team is always just a message away. Welcome aboard! 💚"
        ),
        color=config.COLOR_BRAND,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed
```

#### 3. Rate-Limited DM Delivery (`_dm_welcome`)
Paced with `asyncio.sleep(1)` to respect Discord's rate limits, catching exceptions gracefully for closed DMs:
```python
async def _dm_welcome(members: list[discord.Member]) -> tuple[int, int]:
    success, fail = 0, 0
    for m in members:
        try:
            await m.send(embed=_build_welcome_dm(m))
            success += 1
            await asyncio.sleep(1)  # Rate limit protection
        except Exception:
            fail += 1  # User closed DMs or blocked bot
    return success, fail
```

---

## 3. Custom Reporting System (Member Analytics & Daily Growth)

### Objective
Provide automated, public-facing, self-updating community growth reports across Daily (`vs Yesterday`), Weekly (`vs 7 Days Ago`), and Monthly (`vs 30 Days Ago`) windows, plus a Daily Growth channel report.

### Key Functional Pillars

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               Custom Reporting Engine                                  │
├───────────────────────────────┬───────────────────────────────┬────────────────────────┤
│     A. UTC Snapshots &        │    B. Downtime-Proof Join     │   C. Live Leaderboard  │
│        Baseline Comparison    │       Reconciliation          │      Integration       │
├───────────────────────────────┼───────────────────────────────┼────────────────────────┤
│ • Midnight UTC cron job       │ • on_member_join listener     │ • Rollup from          │
│ • Stores snapshot_date row    │ • Reconciles using member     │   engagement_daily     │
│ • Calculates growth delta     │   joined_at with SQL MAX()    │ • Shows top active     │
│   (Total/Humans/Bots/Online)  │   to fix bot downtime losses  │   members per period   │
└───────────────────────────────┴───────────────────────────────┴────────────────────────┘
```

#### A. Snapshot Recording & Delta Calculation
Snapshots are recorded at `00:05 UTC` daily. The growth delta string uses `_delta_line()`:

```python
def _delta_line(label: str, current: int, previous: int | None) -> str:
    if previous is None:
        return f"{label:<10}│ {current:>5}   (no prior data)"

    delta = current - previous
    if delta > 0:
        arrow, sign = "📈", f"+{delta}"
    elif delta < 0:
        arrow, sign = "📉", f"{delta}"
    else:
        arrow, sign = "➖", "±0"

    pct = f" ({delta / previous * 100:+.1f}%)" if previous > 0 else ""
    return f"{label:<10}│ {previous:>5} → {current:<5} {arrow} {sign}{pct}"
```

Output format rendered inside code blocks:
```text
Total     │  1200 → 1205  📈 +5 (+0.4%)
Members   │  1180 → 1185  📈 +5 (+0.4%)
Bots      │    20 → 20    ➖ ±0
```

#### B. Downtime-Proof Join Reconciliation (`reconcile_joins_from_members`)
Live `on_member_join` events can be missed if the bot is restarting or offline. To guarantee 100% accuracy, the bot scans `member.joined_at` on startup and merges with existing DB join tallies using SQL `MAX()`:

```python
async def reconcile_joins_from_members(bot: commands.Bot, guild: discord.Guild) -> int:
    counts = defaultdict(int)
    for m in guild.members:
        if m.bot or m.joined_at is None: continue
        day = m.joined_at.astimezone(datetime.timezone.utc).date().isoformat()
        counts[day] += 1

    for day, c in counts.items():
        await bot.database.execute(
            "INSERT INTO member_joins (guild_id, period, period_date, joins) "
            "VALUES (?, 'day', ?, ?) "
            "ON CONFLICT(guild_id, period, period_date) "
            "DO UPDATE SET joins = MAX(joins, excluded.joins)",
            (guild.id, day, c),
        )
    return len(counts)
```

#### C. Auto-Refreshing Public Reports Engine
When a moderator deploys a public report via the Mod Panel, a row is recorded in `member_reports`. A background scheduler job re-calculates statistics and edits the existing Discord message every 3 hours (and on bot startup):

```python
async def refresh_one_report(bot: commands.Bot, row) -> bool:
    guild = bot.get_guild(row["guild_id"])
    channel = guild.get_channel(row["channel_id"])
    message = await channel.fetch_message(row["message_id"])

    stats = await record_snapshot(bot, guild)
    baseline = await get_baseline(bot, guild.id, TIMEFRAME_DAYS[row["timeframe"]])
    daily_joins = await get_daily_joins(bot, guild.id, 14)
    monthly_joins = await get_monthly_joins(bot, guild.id, 4)
    prev_month_mtd = await get_prev_month_to_date(bot, guild.id)
    top_active = (await compute_engagement(bot, guild, TIMEFRAME_DAYS[row["timeframe"]]))[:5]

    embed = build_report_embed(
        guild, row["timeframe"], stats, baseline,
        daily_joins, monthly_joins, prev_month_mtd, top_active
    )
    await message.edit(embed=embed)
    await bot.database.execute(
        "UPDATE member_reports SET updated_at = datetime('now') WHERE guild_id = ? AND timeframe = ?",
        (row["guild_id"], row["timeframe"])
    )
    return True
```

---

## 4. Formatting & Embed Engine

### Core Design System (`utils/embed_builder.py`)

All bot embeds conform to a unified brand system with consistent hex colors and emoji conventions:

```python
COLOR_BRAND   = 0x00C9A7  # Teal / Brand Accent
COLOR_SUCCESS = 0x2ECC71  # Emerald Green
COLOR_ERROR   = 0xE74C3C  # Crimson Red
COLOR_WARNING = 0xF1C40F  # Amber Yellow
COLOR_INFO    = 0x3498DB  # Sapphire Blue
```

#### Visual Formatting Standards
- **Section Headers**: `◈` (e.g. `◈  Section Title`)
- **List Items**: `▸` (e.g. `▸ Sub-item detail`)
- **Separators**: `SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"`
- **Status Badges**: `✅` (Healthy), `⚠️` (Warning), `❌` (Error)
- **Footer Format**: `{BOT_NAME} • {BOT_TAGLINE} • DD/MM/YYYY HH:MM AM/PM`

### ANSI Code Block Status Displays
For rich in-embed status views (like `/status`), the bot uses Discord's native ANSI code block syntax:

```python
health_lines = ["**🩺 System Health**", "", "```ansi"]
for label, state in components.items():
    padded_label = f"{label:<22}"
    ansi_yellow = "\u001b[0;33m"
    ansi_reset = "\u001b[0m"
    prefix = "✓" if state in HEALTHY_STATES else "✗"
    health_lines.append(f"{prefix} {padded_label} {ansi_yellow}{state}{ansi_reset}")
health_lines.append("```")
```

### Dynamic Variable Hydration Engine (`core/embed_script.py`)

When creating custom broadcasts or onboarding messages via the Embed Editor, text fields are parsed through `_resolve_text()` to substitute live placeholders dynamically:

```python
def _resolve_text(self, text: Optional[str], member: Optional[discord.Member] = None) -> Optional[str]:
    if not text or "{" not in text:
        return text

    guild = member.guild if member else None
    
    replacements = {
        "{user_mention}": member.mention if member else "(member mention)",
        "{user_name}": member.display_name if member else "(member name)",
        "{server_name}": guild.name if guild else "(server name)",
        "{member_count}": str(guild.member_count) if guild else "0",
        "{date_now}": datetime.now().strftime("%Y-%m-%d"),
    }

    for key, val in replacements.items():
        text = text.replace(key, val)
    return text
```

### Multi-Embed State Management (`EmbedScript`)

The bot supports complex broadcasts containing up to **10 embeds** per message, complete with link buttons and role assignment buttons:

```python
class EmbedScript:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.content: Optional[str] = None
        self.buttons: List[Dict[str, Any]] = []
        self.channels: List[discord.TextChannel] = []
        self.embeds: List[Dict[str, Any]] = [self._default_embed_state()]

    def to_dict(self) -> Dict[str, Any]:
        """Serializes complete message state for DB storage or JSON export."""
        return {
            "content": self.content,
            "embeds": copy.deepcopy(self.embeds),
            "buttons": copy.deepcopy(self.buttons),
            "target_channel_ids": [c.id for c in self.channels if hasattr(c, "id")],
        }
```

---

## 5. Step-by-Step Duplication Checklist for Developers

To duplicate these features in another bot or workspace, follow these steps in order:

### Step 1: Database Setup
1. Execute the DDL queries listed in Section 1 using an async SQLite library (`aiosqlite`).
2. Ensure `journal_mode=WAL` and `foreign_keys=ON` are set on connect.

### Step 2: Implement Embed Utilities & Script Hydrator
1. Create `utils/embed_builder.py` with standard `base_embed`, `success_embed`, `error_embed`, `warning_embed`, `info_embed` helpers using `0x00C9A7`.
2. Create `core/embed_script.py` with `_resolve_text()` variable replacement logic and JSON serialization (`to_dict` / `from_dict`).

### Step 3: Implement Onboarding Cog (`cogs/onboarding.py`)
1. Create `OnboardingHubView` with `btn_scan()` scanning `guild.members` against `onboarded_members`.
2. Add `OnboardingGroupSelect` to cluster join dates.
3. Add `OnboardingComposerModal` to accept text input.
4. Implement `_build_welcome_dm()` with direct `discord.com/channels/...` deep-links.
5. Implement `_dm_welcome()` with `asyncio.sleep(1)` rate-limit protection.

### Step 4: Implement Reporting Cog (`cogs/memberreport.py`)
1. Add `on_member_join` listener writing to `member_joins`.
2. Register a midnight UTC cron job (`5 0 * * *`) calling `record_snapshot()` and `reconcile_joins_from_members()`.
3. Register an interval job (every 3 hours) calling `refresh_all_reports()`.
4. Create `MemberReportHubView` allowing admins to preview or deploy public reports.

### Step 5: Register Cogs & Views in `bot.py`
1. Load `OnboardingCog` and `MemberReportCog` via `bot.add_cog()`.
2. Add persistent views (`OnboardingHubView`, `MemberReportHubView`) in `cog_load()` so UI buttons survive bot restarts.

---

*Document authored by Aljay Leodones for True Classic Bot Architecture Reference.*
