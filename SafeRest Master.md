# SafeRest Master

The complete guide to how the SafeRest Discord bot works, and how to clone it for another brand
in about 15 minutes. SafeRest itself is a clone of the CGK Linens bot.

Built with Python, `discord.py` (slash commands and persistent views) and SQLite.

---

## Table of Contents
1. [Fast Clone: new brand in about 15 minutes](#1-fast-clone-new-brand-in-about-15-minutes)
2. [What the bot does](#2-what-the-bot-does)
3. [Project layout](#3-project-layout)
4. [How it works under the hood](#4-how-it-works-under-the-hood)
5. [Configuration reference](#5-configuration-reference)
6. [Per-brand checklist: everything to change](#6-per-brand-checklist-everything-to-change)
7. [Discord Developer Portal setup](#7-discord-developer-portal-setup)
8. [Discord server setup (roles, channels, permissions)](#8-discord-server-setup)
9. [Slack questionnaire integration](#9-slack-questionnaire-integration)
10. [Running, hosting and updating](#10-running-hosting-and-updating)
11. [Day-to-day moderator usage](#11-day-to-day-moderator-usage)
12. [Troubleshooting](#12-troubleshooting)
13. [Security rules](#13-security-rules)

---

## 1. Fast Clone: new brand in about 15 minutes

> One bot application per brand. Never share a token between brands. Each clone has its own
> folder, `.env`, and database files.

### Step A: Copy the code
```powershell
cd A:\Python
git clone <this-repo-url> new-brand
cd new-brand
Remove-Item -Recurse -Force .git             # detach from the SafeRest history
git init; git add .; git commit -m "Initial clone from SafeRest Master"
# Optional: create a new private GitHub repo and push to it
```
Do **not** copy `.env` or `data/`. They are gitignored, so a clean clone won't include them.

### Step B: Create the new Discord bot
Follow [section 7](#7-discord-developer-portal-setup). You need the **token**, the three
**privileged intents** and the **invite link**.

### Step C: Create the virtual environment
```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### Step D: Configure `.env`
```powershell
Copy-Item .env.example .env
```
Fill in at least:
```dotenv
DISCORD_TOKEN=<new bot token>
GUILD_ID=<the new server's ID>          # instant slash-command sync
REPORT_CHANNEL_ID=<#reports channel>
REPORT_TIMEZONE=Asia/Manila             # change if the brand's team is elsewhere
SLACK_ONBOARDING_WEBHOOK_URL=           # optional, see section 9
```
**Important:** `.env.example` and `config.py` still contain SafeRest's channel and role IDs as defaults.
Blank them out or replace them (see [section 6](#6-per-brand-checklist-everything-to-change)).
Otherwise the new bot will try to use SafeRest IDs that don't exist in the new server.

### Step E: Rebrand (3 edits in `config.py`)
```python
BRAND_NAME = "New Brand"
BOT_NAME = "New Brand Bot"
BRAND_COLOR = 0x...                      # brand hex colour
EMBED_FOOTER_TEXT = "New Brand Management Bot"
```
Also replace the `CONTEST_EXCLUDED_ROLE_IDS` and staff role IDs (section 6). Contest channels,
the leaderboard channel and the questionnaire channel are all chosen inside Discord.

### Step F: Run
```powershell
.\venv\Scripts\python.exe main.py        # or start_bot.cmd
```
In Discord, run `/summon` in a staff channel. The persistent **Mod Panel** appears. Open
**Onboarding, Information Onboarding, SafeRest Onboarding, Engagement Contest -> Settings** and pick the new
server's channels and roles there. **Settings saved in Discord always override `.env`.**

### Step G: Smoke test
- [ ] Bot is online and `/summon` works
- [ ] Mod Panel buttons respond
- [ ] A test account joins, gets a private onboarding channel, and finishes it
- [ ] Questionnaire answers arrive in Slack (if enabled)
- [ ] Test contest round starts, scores and publishes a leaderboard

---

## 2. What the bot does

Everything is driven from the **Mod Panel** (`/summon`). It is persistent, so its buttons keep working
after restarts.

| System | Purpose |
| --- | --- |
| **SafeRest Onboarding** | Creates a private `<handle>-onboarding` channel per new member in a category. The member answers the TikTok Shop questions, accepts the rules and completes the questionnaire. The bot then grants the **access role** that unlocks the server. |
| **Creator Onboarding** | Tags recent joiners in one post asking "Are you a TikTok Shop creator?" with YES/NO buttons. Each member gets a private tailored reply, including links to the getting-started and opportunities channels. |
| **Information Onboarding** | Finds members who joined recently and were never welcomed, lets staff choose which join days to include, posts a tagged welcome and DMs a branded message. Each welcomed member is logged so nobody is welcomed twice. |
| **Questionnaire** | The member questionnaire posted publicly in a fixed channel. Answers are forwarded privately to Slack through a webhook. |
| **Community Reports** | Daily growth baseline, joins per day, monthly trends and most active members. One public report refreshes itself every `REPORT_REFRESH_HOURS`. |
| **Engagement Reports** | Ranks members by messages, reactions and threads over any date range, with a formal summary and full text export. |
| **Engagement Contest** | Start and end rounds. It scores genuine engagement in the contest channels (default 2 pts per message, 1 pt per reaction, 10 pts per TikTok video), filters spam and copy-paste, publishes a Top 15 leaderboard and keeps history. |
| **Announcements, Broadcast, Embed Studio, Message Editor** | Compose and post branded announcements and embeds, send plain messages, and edit previously posted bot messages. Banners are stored locally in `data/banners`. |

---

## 3. Project layout

```
main.py                     Entry point: loads cogs, registers persistent views, runs the bot
config.py                   ALL settings: env vars, brand name/colours, hardcoded channel IDs
.env / .env.example         Secrets and per-environment values (.env is never committed)
start_bot.cmd / .command    Double-click launchers (Windows / macOS)
export_onboarding.py        Export onboarding data
check_slack_connection.py   Test the Slack webhook
requirements.txt            Python dependencies

cogs/                       Features (one Discord "cog" each)
  mod_panel.py              /summon and the main panel
  saferest_onboarding.py         Private onboarding channels and access role
  creator_onboarding.py     TikTok Shop creator question
  onboarding.py             Welcome and DM scanner
  questionnaire.py          Questionnaire and Slack delivery
  engagement.py             Activity tracking and reports
  engagement_contest.py     Contest scoring engine
  memberreport.py           Community growth reports
  announcements.py          Announcement panel
  message_editor.py         Edit bot messages

views/                      Buttons, modals, selects (the Mod Panel UI)
core/                       database.py (SQLite), embed templates, images, questionnaire content
utils/embed_builder.py      One shared embed style (colours and footer)
tests/                      Automated tests
data/                       Runtime SQLite databases, banners, exports (gitignored)
```

---

## 4. How it works under the hood

1. **Startup (`main.py`)**: loads `.env` through `config.py`, builds `SafeRestBot`, loads every cog in
   `EXTENSIONS`, and re-registers persistent views (Mod Panel, onboarding hubs, creator buttons) so old
   messages keep working.
2. **Intents**: needs Server Members and Message Content. Presence is optional. If it is off in the
   portal, the bot falls back to running without it automatically.
3. **Slash commands**: synced to `GUILD_ID` if set (instant), otherwise globally (up to about an hour).
4. **Settings precedence**: *saved Mod Panel settings (database)* beat *`.env`*, which beats the
   *defaults in `config.py`*. A brand can therefore be configured entirely from inside Discord.
5. **Storage**: two SQLite databases in `data/`:
   - `engagement.sqlite3`: message, reaction and thread activity, and contests
   - `community.sqlite3`: onboarding state, welcome log, report snapshots, saved settings
   
   Back them up by copying the files while the bot is stopped.
6. **Contest scoring**: a message only scores if it clears these gates: in a contest channel, author has no
   excluded role, at least `CONTEST_MIN_MESSAGE_WORDS` real words (links, mentions and emoji stripped),
   not a repeat by the same user within `CONTEST_SELF_REPEAT_HOURS`, not copied between users within
   `CONTEST_CROSS_REPEAT_MINUTES`, and past the `CONTEST_MESSAGE_COOLDOWN_SECONDS` spam cooldown.
7. **Embeds**: all styling comes from `config.py` colours and `utils/embed_builder.py`, so a rebrand only
   needs the config constants.

---

## 5. Configuration reference

All of these are in `.env` (see `.env.example`). Everything except the token is optional.

| Variable | Purpose | Default |
| --- | --- | --- |
| `DISCORD_TOKEN` | **Required.** Bot token | none |
| `GUILD_ID` | Server ID for instant command sync | blank |
| `REPORT_CHANNEL_ID` | Engagement report destination | SafeRest channel |
| `REPORT_TIMEZONE` | IANA timezone for report dates | `Asia/Manila` |
| `ACTIVITY_DATABASE_PATH` | Activity DB | `data/engagement.sqlite3` |
| `COMMUNITY_DATABASE_PATH` | Onboarding and growth DB | `data/community.sqlite3` |
| `BANNER_ASSET_PATH` | Uploaded banners | `data/banners` |
| `CONTEST_REPORT_CHANNEL_ID` | Leaderboard destination (Engagement Contest → Settings wins) | blank |
| `CONTEST_POINTS_MESSAGE / _REACTION / _TIKTOK` | Point values | 2 / 1 / 10 |
| `CONTEST_MIN_MESSAGE_WORDS` | Min real words | 3 |
| `CONTEST_SELF_REPEAT_HOURS` | Self-repeat window | 24 |
| `CONTEST_CROSS_REPEAT_MINUTES` | Copy-paste window | 60 |
| `CONTEST_MESSAGE_COOLDOWN_SECONDS` | Spam cooldown | 10 |
| `CONTEST_HISTORY_LIMIT` | Past contests listed | 25 |
| `WELCOME_CHANNEL_ID`, `ONBOARDING_CHANNEL_ID`, `INTRODUCTIONS_CHANNEL_ID` | Information Onboarding channels | blank |
| `CREATOR_WELCOME_CHANNEL_ID` | Where the creator question is posted | blank |
| `CREATOR_GETTING_STARTED_CHANNEL_ID` | Channel for learning creators | SafeRest channel |
| `CREATOR_OPPORTUNITIES_CHANNEL_ID` | Linked after finishing | SafeRest channel |
| `SAFEREST_ONBOARDING_CATEGORY_ID` | Category for private onboarding channels | SafeRest category |
| `SAFEREST_ONBOARDING_ROLE_ID` | Access role granted on completion | SafeRest role |
| `ONBOARDING_LOOKBACK_DAYS` | How far back the scanner looks | 30 |
| `REPORT_REFRESH_HOURS` | Live report refresh interval | 3 |
| `ENABLE_PRESENCE_INTENT` | "Online now" metric | `true` |
| `SLACK_ONBOARDING_WEBHOOK_URL` | Slack incoming webhook (secret) | blank |

To get any ID: Discord **User Settings -> Advanced -> Developer Mode**, then right-click the server,
channel or role and choose **Copy ID**.

---

## 6. Per-brand checklist: everything to change

These are the SafeRest-specific values currently baked into the code. **Change each one in a clone.**

| File | What | Action |
| --- | --- | --- |
| `.env` | token, `GUILD_ID`, channel/role IDs | Fill with the new brand's values |
| `.env.example` | SafeRest channel/role IDs shown as examples | Replace with blanks in the clone |
| `config.py` | `BRAND_NAME`, `BOT_NAME`, `BRAND_COLOR`, `EMBED_FOOTER_TEXT` | Rebrand |
| `config.py` | `REPORT_CHANNEL_ID`, `CONTEST_REPORT_CHANNEL_ID`, `CREATOR_*`, `SAFEREST_ONBOARDING_*` fallback defaults | Replace with the new IDs (or `None`) |
| `config.py` | `CONTEST_EXCLUDED_ROLE_IDS` (staff roles that can't score) | New server's staff roles |
| `core/questionnaire.py` | Questionnaire questions and wording | Adapt to the brand (e.g. hotel guests are not TikTok creators) |
| `cogs/creator_onboarding.py`, `views/creator_onboarding_view.py` | TikTok Shop wording | Rewrite or remove the cog from `EXTENSIONS` if it isn't relevant |
| `cogs/saferest_onboarding.py` | "SafeRest team" text and topic template | Rename wording |
| `cogs/announcements.py` | `/...` description says "SafeRest Discord Announcement" | Rename |
| `main.py` | `SafeRestBot` class and `saferest_bot` logger name | Optional cosmetic rename |
| `README.md` | Brand text | Update |

> [!TIP]
> Search the clone for `SafeRest`, `Linens`, `Aljay` and any 17-19 digit number to catch every leftover.
> ```powershell
> Select-String -Path *.py,cogs\*.py,core\*.py,views\*.py,utils\*.py,*.md -Pattern "SafeRest|Linens|\b\d{17,19}\b"
> ```

**Turning features off for a brand:** remove the cog's line from `EXTENSIONS` in `main.py`. For example,
a brand may not need `cogs.creator_onboarding`. If you do, also remove the matching
`add_view(...)` line in `main.py` and the panel button that opens it.

### SafeRest quick reference

| | SafeRest |
| --- | --- |
| Folder | `saferest` |
| Bot application | `SafeRest Bot` (ID `1559227125750562917`) |
| Slack channel | create e.g. `saferest-discord-data` |
| Onboarding category ID | `1559588851381575732` |
| Admin role ID | `1549192877358325941` |
| Team role ID | `1559235558037790790` |
| Verified role ID | `1559235486361583776` (default; change in SafeRest Onboarding → ⚙️ Settings) |
| Bot role ID | `1559588437068087356` |
| Member role ID (granted after onboarding) | `1559588345598972116` |
| Questionnaire channel | picked each time in Mod Panel → 📋 Post Questionnaire |
| Contest channels | picked in Mod Panel → 🏆 Engagement Contest → ⚙️ Settings |

---

## 7. Discord Developer Portal setup

1. Open https://discord.com/developers/applications -> **New Application** -> name it (e.g. `SafeRest Bot`).
2. **Bot** tab -> under **Privileged Gateway Intents** enable **Presence**, **Server Members** and
   **Message Content** -> **Save Changes**.
3. **Bot** tab -> **Reset Token** -> copy it into `.env` as `DISCORD_TOKEN`.
4. **OAuth2 -> URL Generator**. Scopes: `bot` and `applications.commands`. Bot permissions:
   - Manage Roles, Manage Channels
   - View Channels, Send Messages, Send Messages in Threads
   - Embed Links, Attach Files, Read Message History, Add Reactions
   - Manage Messages, Manage Threads
5. Open the generated URL and invite the bot to the new server.

---

## 8. Discord server setup

- **Bot role position**: drag the bot's role **above** the onboarding access role. Otherwise it cannot assign it.
- **Access role**: the role that unlocks the main channels. Hide the main channels from `@everyone` and
  show them to this role only.
- **Onboarding category**: a category where the bot creates private channels. Give the bot **Manage
  Channels** and **Manage Permissions** there. Staff roles should be able to view it.
- **Channels needed**: staff channel (for `/summon`), reports channel, contest leaderboard channel,
  welcome/introductions channels, questionnaire channel.
- Then set all of these in the Mod Panel -> **Settings** pages of each system.

---

## 9. Slack questionnaire integration

Questionnaire answers are delivered privately to Slack through an **incoming webhook**, one per brand.

1. A Slack workspace admin opens https://api.slack.com/apps -> **Create New App -> From scratch**
   (e.g. `Discord Onboarding`).
2. **Incoming Webhooks** -> activate -> **Add New Webhook to Workspace** -> pick the brand's private channel
   (for example `saferest-discord-data`) and authorize.
3. Copy the URL into that brand's `.env`:
   ```dotenv
   SLACK_ONBOARDING_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```
4. Restart the bot and verify:
   ```powershell
   .\venv\Scripts\python.exe check_slack_connection.py
   ```

One Slack app can provide several webhooks, but each URL is bound to its channel. See
[SLACK_ONBOARDING_SETUP.md](SLACK_ONBOARDING_SETUP.md) for more detail. Leaving the variable blank
disables Slack delivery without breaking the questionnaire.

---

## 10. Running, hosting and updating

- **Run**: `.\venv\Scripts\python.exe main.py`, or double-click `start_bot.cmd` (Windows) or
  `start_bot.command` (macOS).
- **Keep it online 24/7**: run on a small VPS or always-on PC. Use Task Scheduler, `pm2`, `nssm` or `systemd`
  so it restarts on crash and boot. Note that the bot is only online while the process runs.
- **Update a clone with new SafeRest features**: add the SafeRest repo as a remote and cherry-pick, or copy changed
  files. Avoid overwriting `config.py` and the questionnaire content.
  ```powershell
  git remote add saferest <saferest-repo-url>
  git fetch saferest
  git diff HEAD saferest/main -- cogs views core   # review, then copy what you want
  ```
- **Backup**: stop the bot and copy the `data/` folder. Never commit it.
- **Tests**: `.\venv\Scripts\python.exe -m pytest tests`

---

## 11. Day-to-day moderator usage

| Task | How |
| --- | --- |
| Open the control panel | `/summon` in a staff channel |
| Welcome new members | Mod Panel -> Information Onboarding -> choose days -> send |
| Ask the creator question | Mod Panel -> Onboarding -> post |
| Change channels/roles | Mod Panel -> the system -> **Settings** |
| Run a contest | Mod Panel -> Engagement Contest -> Start; later End, then Publish the Top 15 |
| Engagement report | Mod Panel -> Engagement Reports -> pick the date range |
| Community report | Mod Panel -> Community Reports -> Deploy (it auto-refreshes) |
| Announcement or embed | Mod Panel -> Announcements or Embed Studio; edit later with Message Editor |

---

## 12. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Bot online but no slash commands | Set `GUILD_ID` for instant sync, or wait for global sync. Re-invite with `applications.commands`. |
| `PrivilegedIntentsRequired` | Enable all intents in the Developer Portal. Presence can be skipped (`ENABLE_PRESENCE_INTENT=false`). |
| Can't assign access role | Move the bot's role above the access role and grant Manage Roles. |
| Private channels not created | Check the category ID and the bot's Manage Channels permission in that category. |
| Wrong or missing channels in a clone | Leftover SafeRest IDs. Run the `Select-String` search from section 6. |
| Slack gets nothing | Run `check_slack_connection.py`, check the webhook URL and restart the bot. |
| Contest scores nobody | Check Engagement Contest → ⚙️ Settings (counted channels), the excluded roles and the min-words rule. |
| `Improper token` | Reset the token in the portal and update `.env`. |
| Garbled characters in terminal output | Console encoding only. Run `chcp 65001`. The files are fine. |

---

## 13. Security rules

- `.env` and `data/` are gitignored. **Never commit them**, and never paste tokens or webhook URLs into
  Discord, screenshots or docs.
- If a token leaks, **Reset Token** in the Developer Portal immediately.
- Keep one token and one Slack webhook per brand. If one is compromised, the others are unaffected.
- Give the bot only the permissions it needs, and keep its role below the owner and admin roles.

---

*Created by Aljay Leodones. Maintained as the single source of truth for cloning this bot.*
