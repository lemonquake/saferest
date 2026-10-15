# SafeRest Discord Bot

A management Discord Bot built for SafeRest.
Created by Aljay Leodones.

---

## What This Bot Does

This bot provides server management, announcement, onboarding, and member engagement tools for the SafeRest Discord community. It includes a persistent **Mod Panel** summoned with `/summon` that stays active even after bot restarts.

Three systems power it:

| System | What it does |
| --- | --- |
| **SafeRest Onboarding** | Opens a private, ticket-style **#handle-onboarding** channel for each new member, walks them through the TikTok Shop questions, the community rules and the Slack-connected questionnaire, then grants the access role that unlocks the rest of the server. |
| **Creator Onboarding** | Opens a private **#handle-onboarding** channel for each recently joined member and tags them there with *Are you a TikTok Shop creator?* (**✅ YES / ❌ NO**). The member works through onboarding in that channel and can chat with the team there directly. |
| **Information Onboarding** | Scans for members who joined recently and were never welcomed, lets you pick **which join days** to include, then posts a tagged welcome and sends each member a warm branded DM. Everyone welcomed is logged so nobody is welcomed twice. |
| **Community Reports** | Daily growth baselines, joins per day, monthly trends and most-active members. Deploy one public report and it refreshes itself in place every few hours. |
| **Engagement Reports** | Ranks every member by messages, reactions and threads for any date range, published as a formal summary plus a full text file. |
| **Engagement Contest** | Start/end contest rounds, score genuine engagement across the contest channels you choose in ⚙️ Settings (2 pts per genuine message, 1 pt per reaction, 10 pts per TikTok video post), filter out spam and copy-paste, publish the Top 15 leaderboard, and browse past contests. |

All of it is driven from the Mod Panel — no commands to memorise.

---

## Step-by-Step Installation & Setup Guide

Follow these numbered steps to set up and run the bot.

---

### Phase 1: Discord Developer Portal Setup

#### Step 1: Open the Discord Developer Portal
1. Open your web browser and go to: [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Log in with your Discord account.

#### Step 2: Select or Create Your Bot Application
1. Click the **"New Application"** button in the top-right corner (or click your existing application if already created).
2. Give your application a name (for example, `SafeRest Bot`) and agree to the Discord Developer terms.
3. Click **Create**.

#### Step 3: Enable Privileged Gateway Intents (Very Important)
1. In the left sidebar, click on **"Bot"**.
2. Scroll down to the section titled **"Privileged Gateway Intents"**.
3. Turn **ON** (check) all three switches:
   - **Presence Intent**
   - **Server Members Intent**
   - **Message Content Intent**
4. Click **"Save Changes"** at the bottom of the page.

#### Step 4: Get Your Bot Token
1. On the same **"Bot"** page, find the **"Token"** section.
2. Click **"Reset Token"** (or **"Copy"** if freshly generated).
3. Copy this token. This is the secret password for your bot.

#### Step 5: Create the Bot Invite Link
1. In the left sidebar, click on **"OAuth2"**, then click on **"URL Generator"**.
2. Under **"Scopes"**, check these two boxes:
   - `bot`
   - `applications.commands` (this enables slash commands like `/summon`)
3. Under **"Bot Permissions"**, check:
   - `Administrator` (or select `Manage Server`, `Send Messages`, `Embed Links`, `Read Message History`, `Use Slash Commands`).
   - For engagement reports without Administrator access, grant `View Channels` and `Read Message History` in every channel that should be analyzed, plus `Send Messages`, `Embed Links`, and `Attach Files` in the report channel.
4. Scroll to the bottom and click **"Copy"** next to the **Generated URL**.

#### Step 6: Invite the Bot to Your Discord Server
1. Paste the copied URL into a new tab in your web browser.
2. Select your Discord server from the dropdown list.
3. Click **"Continue"**, then click **"Authorize"**.
4. Complete the verification check. The bot will now appear in your server member list.

---

### Phase 2: Running the Bot on Your Computer

### Quick Start Launchers (Easiest Way to Run)

- **On Mac:** Simply double-click [`start_bot.command`](file:///Users/lemonquake/Engines/saferest-bot/start_bot.command). It automatically checks the virtual environment, verifies requirements, and starts the bot.
- **On Windows:** Simply double-click [`start_bot.cmd`](file:///Users/lemonquake/Engines/saferest-bot/start_bot.cmd).

---

### Manual Terminal Setup (Step-by-Step)

#### Step 7: Open Your Terminal
Open the Terminal application on your computer and navigate to the project directory:
```bash
cd /Users/lemonquake/Engines/saferest-bot
```

#### Step 8: Set Up Python Virtual Environment
Create and activate a virtual environment to manage dependencies cleanly:
```bash
python3 -m venv venv
source venv/bin/activate
```

#### Step 9: Install Required Dependencies
Install the required packages (`discord.py` and `python-dotenv`):
```bash
pip install -r requirements.txt
```

#### Step 10: Configure Your `.env` File
1. Make sure you have a `.env` file in the project root directory.
2. Open `.env` and add your bot token:
```env
DISCORD_TOKEN=your_token_here
GUILD_ID=
REPORT_CHANNEL_ID=<your #reports channel ID>
REPORT_TIMEZONE=Asia/Manila
```
*(Note: If you want slash commands to appear instantly in a specific test server while developing, paste your Server/Guild ID in `GUILD_ID`)*.

Everything else is optional — see [`.env.example`](.env.example) for the full list:

| Setting | Default | Purpose |
| --- | --- | --- |
| `WELCOME_CHANNEL_ID` | *(blank)* | Starting value for the welcome post channel. |
| `ONBOARDING_CHANNEL_ID` | *(blank)* | The "start here" channel linked in welcomes. |
| `INTRODUCTIONS_CHANNEL_ID` | *(blank)* | The introductions channel linked in welcomes. |
| `ONBOARDING_LOOKBACK_DAYS` | `30` | How far back the onboarding scan looks. |
| `REPORT_REFRESH_HOURS` | `3` | How often live community reports refresh. |
| `ENABLE_PRESENCE_INTENT` | `true` | Tracks "online now"; the bot starts without it automatically if the portal switch is off. |
| `COMMUNITY_DATABASE_PATH` | `data/community.sqlite3` | Onboarding log, growth snapshots, live reports. |

You never have to edit `.env` for the three onboarding channels — set them in Discord from **Mod Panel → Onboarding → ⚙️ Settings** (or press **⭐ Make default** while sending a welcome), and those saved choices always win.

#### Step 11: Start the Bot
Run the bot with:
```bash
python3 main.py
```
You will see output indicating that the bot has connected:
```
[INFO] saferest_bot: Registering persistent views...
[INFO] saferest_bot: Loading extensions...
[INFO] saferest_bot.engagement: Engagement tracking database ready at data/engagement.sqlite3
[INFO] saferest_bot.database: Community database ready at data/community.sqlite3
[INFO] saferest_bot.onboarding: Onboarding ready (lookback: 30 days)
[INFO] saferest_bot.memberreport: Community reporting ready (live reports refresh every 3h)
[INFO] saferest_bot: Synced 3 global command(s).
[INFO] saferest_bot: ==========================================
[INFO] saferest_bot: Logged in as: SafeRest Bot (ID: ...)
[INFO] saferest_bot: Bot is active in 1 server(s)
[INFO] saferest_bot: Presence tracking: on
[INFO] saferest_bot: ==========================================
```

---

### Phase 3: Using the Bot

#### Step 12: Summon the Mod Panel
1. Go to any text channel in your Discord server where you have moderator/admin permissions.
2. Type `/summon` and press Enter.
3. The **SafeRest Management Panel** appears with live server info, bot latency, an onboarding snapshot, your live reports, a plain-English guide to every button, and a system health block.
4. Buttons on the panel:

| Button | What it opens |
| --- | --- |
| **🎟️ SafeRest Onboarding** | Scan new members and open a private onboarding channel for each. **🧪 Test on myself** opens one for you. **📣 Post self-onboarding button** lets members open their own. |
| **👋 Onboarding** | Creator onboarding: open a private onboarding channel for each new member and tag them there with the TikTok Shop question (✅ YES / ❌ NO). **🧪 Test message** sends it to any channel. |
| **ℹ️ Information Onboarding** | Scan for new members, pick the join days and the channel to post in, send the welcome. Set a default channel and message. |
| **📊 Community Reports** | Preview or deploy self-refreshing growth reports. |
| **📈 Engagement Report** | Rank members by messages, reactions and threads. |
| **🏆 Engagement Contest** | Choose the counted channels and leaderboard channel (⚙️ Settings), start a new contest, publish the Top 15 leaderboard, end a contest, review past contests, or reset all counts. |
| **📝 Embed Message** | Open the Embed Studio: compose, save reusable templates, and publish to any channel. |
| **💬 Normal Message** | Write a plain message, preview it, edit it, attach up to 4 images, then post it. |
| **🔄 Refresh Dashboard** | Reload the panel with the latest data **and the latest buttons**. |

> Already have an older panel posted? Just click **🔄 Refresh Dashboard** on it once — it upgrades that message to the new button set in place.

Two optional shortcuts post standalone panels for teams that prefer them: `/onboarding` and `/community-report`. Both are persistent, just like `/summon`.

---

#### Step 12a: SafeRest Onboarding (private onboarding channels)

1. On the Mod Panel click **🎟️ SafeRest Onboarding**.
2. *(First time)* check **⚙️ Settings**. It already points to the onboarding category (`1559588851381575732`) and the Member access role (`1559588345598972116`). Anything that would block it is listed there — typically:
   - the bot needs **Manage Channels** in the category and **Manage Roles**,
   - the bot's own role must sit **above** the access role in *Server Settings → Roles*.
3. Click **🧪 Test on myself** — or type `/test-onboarding` (add `member:` to test it on someone else). The bot opens a private onboarding channel so you can click through the whole flow. Submitting the questionnaire really saves the answers for Slack and grants the role. Nobody is added to a tagged group. Press **🔒 Close onboarding** in the channel when you're done.
4. Click **🔍 Scan for new members**. Members who joined in the last 30 days, haven't been tagged yet and don't already have the access role are grouped by join day. Pick the days and press **🎟️ Open N onboarding channels**.
5. Need to tag a group again? Scan, then press **♻️ Clear last tagged group & rescan**. The most recent group goes back in the list, and tagging them again re-uses their existing channels.
6. *(Optional)* **📌 Post Shop 101 checkpoint** posts an *I've finished TikTok Shop 101* button in #🛍️tiktok-shop-101, so 🌱 creators can continue from there.
7. *(Optional, self-serve)* **📣 Post self-onboarding button**, or `/self-onboarding`, posts a public **🎟️ Start my onboarding** button in a channel you pick. Anyone who presses it gets their own private onboarding channel, with no moderator needed. A member who already has a channel is pointed back to it. A member who already has the access role is told they're all set. Double clicks never open two channels. Self-starters are counted in the hub's **Started it themselves**, and scans skip them. Post the button in a channel that new members (@everyone) can see.

What each member gets:

```
#lemon-onboarding  (only the member and your moderators can see it)
  @lemon 🔒 This is your private onboarding channel … finish the onboarding below
  to unlock all of the server's channels.
  👋 Welcome to the SafeRest Creator Community! … Are you a TikTok Shop creator?
  [✅ YES] [❌ NO]   → same flow as below; replies stay in the channel
  [🔒 Close onboarding]
```

**Closing a channel:** the member or any moderator can press **🔒 Close onboarding** on the first message. After a quick "are you sure?" the bot deletes the channel. A channel a moderator deletes by hand in Discord is forgotten the same way, and tagging that member again opens a fresh channel.

Only the channel's member can answer in it. Submitting the questionnaire grants the access role, then posts *"You now have full access to the SafeRest creator community! Head to #opportunities-to-earn …"* in the channel. That completes the workflow. If the role can't be granted, the post says so instead, and a moderator can add it by hand. A category holds at most **50** channels, so close finished onboarding channels from time to time.

---

#### Step 12b: Creator Onboarding (TikTok Shop question)

1. On the Mod Panel click **👋 Onboarding**.
2. *(First time)* click **⚙️ Settings** and pick:
   - **Default test channel** — pre-selected for **🧪 Test message**.
   - **Getting-started channel** — where 🌱 learning creators are sent. Pick your getting-started channel here.
   - **Opportunities channel** — linked in the message members get once they finish. Pick your opportunities channel here.
3. Click **🧪 Test message**, pick a channel and press **Send test message**. The real interactive message is posted there (nobody tagged, nobody logged) so you can click through every answer yourself.
4. Click **🔍 Scan for new members**, pick the join days, then press **✅ Send to N members**. Each member gets their own private **#handle-onboarding** channel in the onboarding category (`1559588851381575732`, changeable in **🎟️ SafeRest Onboarding → ⚙️ Settings**) and is tagged there with the question and **✅ YES / ❌ NO** right below. Only they and the team can see the channel, so they can type any questions there and chat with you directly. Everyone is logged so the next scan skips them; a member who already has a channel is re-tagged in it, never given a second one.

What members see — the replies stay in their own channel (on a 🧪 test post in a shared channel they are private instead):

```
❌ NO   → how to get started with TikTok Shop (1,000 followers, apply at shop.tiktok.com)
✅ YES  → "Where are you in your TikTok Shop journey?"
           🌱 Still learning the ropes  → head to #🛍️tiktok-shop-101
                                          [✅ I've finished TikTok Shop 101] → next step
           🔥 Experienced and ready     → community rules + questionnaire
Questionnaire submitted → answers saved and sent to Slack, access role granted
                         → "You now have full access to the SafeRest creator community!
                            Head to #opportunities-to-earn … 🎉"
```

Creator onboarding keeps its **own** log, separate from Information Onboarding, so a member welcomed by one still shows up in the other's scan. **📋 Stats** shows how many members answered YES, NO, 🌱 and 🔥. The buttons keep working after a restart.

---

#### Step 13: Welcome New Members (Information Onboarding)

1. On the Mod Panel click **ℹ️ Information Onboarding**.
2. *(Optional, first time)* click **⚙️ Settings**:
   - **Default welcome channel** — pre-selected every time you send a welcome. You can still pick a different channel for any single send.
   - **Start-here channel** and **Introductions channel** — linked in every welcome post and DM.
   - **Edit default message** — the welcome text every send starts from. **Use built-in message** puts the original back.
   Each choice saves instantly and survives restarts. Clear a menu to unset that channel.
3. Click **🔍 Scan for new members**. Everyone who joined in the last 30 days and was never welcomed opens in one **welcome composer**, grouped by join day:
   ```
   ▸ Joined today — 4
   ▸ Joined yesterday — 2
   ▸ Joined 1 week ago — 9
   ```
4. Everything is on that one screen, and nothing is sent until you press **Send**:
   - **Join days** — pick one day, several, or **Everyone not yet welcomed**.
   - **Channel** — where the post goes. It opens on your default channel; pick any other channel to post there instead. Press **⭐ Make default** to remember the one you picked.
   - **Delivery** — **📣 Post + warm DM** (recommended), **📢 Post only** or **✉️ Warm DM only**.
   - **✏️ Edit message** — change the text for this send. **💾 Save message** keeps it as the default; **↩️ Undo edits** goes back.
   - A live **post preview** shows exactly what will be posted.
5. Press **✅ Send to N members**. The bot:
   - posts one message tagging every selected member (more tags spill into follow-up messages),
   - DMs each member a branded welcome with clickable links to your start-here and introductions channels,
   - logs them as onboarded so the next scan skips them.

Notes:
- The bot checks it can post in the chosen channel **before** anything is sent, and tells you which permission is missing.
- DMs are paced at **one per second** to respect Discord's rate limits; the progress updates as it goes. The summary reports how many DMs landed and who has DMs closed.
- With **✉️ Warm DM only**, members whose DMs are closed are **not** logged, so they appear in the next scan and can be welcomed with a public post.
- **📋 Onboarding stats** shows how many members are waiting, how many were welcomed this week, the all-time total and your current setup.
- **♻️ Reset onboarding log** clears the log if you ever want to welcome people again.

Placeholders you can use in any message (welcome posts, embeds, plain messages):

```
{user_mention}  {user_name}  {server_name}  {member_count}  {date_now}
{time_now}  {brand}  {onboarding_channel}  {introductions_channel}  {welcome_channel}
```

---

#### Step 14: Deploy a Community Report

1. On the Mod Panel click **📊 Community Reports**.
2. **Select which days** the report covers: today, 7 days, 14 days, 30 days, 90 days, or **Custom number of days** (anything from 1 to 365).
3. Then either:
   - **👀 Preview a report** — private, nothing is posted; or
   - **📌 Deploy live report** — pick a channel and the report goes public.
4. A deployed report **refreshes itself in place** every 3 hours (`REPORT_REFRESH_HOURS`) and on every bot start. You never repost it.
5. **🔄 Refresh now** updates every live report immediately. **🗂️ Manage live reports** stops one from refreshing (the posted message stays).

Each report shows:
- **Membership growth** — total, members, bots and online now, each compared to the stored baseline from that many days ago, with the arrow and percentage change.
- **New members per day** — a 14-day bar chart.
- **Joins trend** — monthly totals plus this month versus the same point last month.
- **Most active members** — top contributors for the window, taken from the engagement tracker.
- **Server snapshot** — members, bots, team/admins and online count.

How the numbers stay accurate:
- A baseline snapshot is recorded at **00:05 UTC** every day, on every refresh, and whenever a report is built.
- Join tallies come from live join events **and** are re-derived from every member's join date on each refresh, merged with `MAX()`. Nothing is lost while the bot is offline.
- "Online now" needs the **Presence Intent**. If that switch is off in the Developer Portal the bot notices, starts without it, and hides just that one metric.

---

#### Step 15: Generate an Engagement Report
1. Open the Mod Panel and click **📈 Engagement Report**.
2. Choose **Past 1 day**, **Past 7 days**, **Past 1 month**, or **Custom date range**.
3. For a custom range, enter `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. Date-only end dates include the complete end day.
4. The bot publishes a formal summary and full ranked text report to `REPORT_CHANNEL_ID`.

The tracker counts each non-bot member's messages, reactions added, and threads created. Each action is one activity point. Reports rank up to 300 members in servers with 300 or fewer members, or up to 1,000 members in larger servers. Before publishing, the bot backfills every readable text channel and active or archived thread for the selected period. Large date ranges can take a few minutes. Discord does not expose historical reaction timestamps, so backfilled reaction totals represent reactions currently visible on messages posted during the selected period; live reaction events use their actual event time.

---

#### Step 15b: Run an Engagement Contest

The contest runs in **rounds**. Starting a contest fixes the starting point of the
count, so nothing posted beforehand can ever score.

1. Open the Mod Panel and click **🏆 Engagement Contest**. The hub shows whether a
   contest is currently running, the scoring rules, and the counted channels.
   *(First time)* press **⚙️ Settings** and pick the **counted channels** (up to 25;
   their threads count too) and the **leaderboard channel**. You can change them at
   any time, even while a contest runs. A contest can't start until at least one
   channel is chosen.
2. Click **🚀 Start New Contest**, give the round a name (e.g. `September Engagement
   Contest`) and submit. Counting begins at that exact moment. If a contest was
   already running it is closed automatically.
3. While it runs, click **🏆 Publish Leaderboard** at any time to scan the contest
   channels and post the current Top 15 standings to the leaderboard channel.
4. Click **🛑 End Contest** to close the round. The final standings are frozen into
   the archive and published as **FINAL RESULTS**.
5. Click **📜 Past Contests** to browse every round on record, view its leaderboard,
   and re-post an archived leaderboard to the report channel.
6. **⚙️ Ad-hoc Report** scores any date range without touching the running contest.
7. **♻️ Reset All Counts** permanently deletes every recorded event and every
   archived contest for the server. Use it only to start the system from scratch —
   to simply begin a new round, use **Start New Contest** instead, which keeps
   old contests archived.

**How points are awarded**

| Action | Points | Conditions |
| --- | --- | --- |
| 💬 Genuine message | **2** | More than two real words. Links, mentions and emoji do not count as words. |
| 🎬 TikTok video post | **10** | Any `tiktok.com`, `vm.`, `vt.` or `m.tiktok.com` link. Each clip scores once per member. |
| ❤️ Reaction | **1** | Every reaction added in a contest channel. |

**What does not score**

- Messages of two words or fewer, and messages that are only a link, mention or emoji.
- The same text repeated by the same member within `CONTEST_SELF_REPEAT_HOURS` (default 24h).
- Text copy-pasted from another member within `CONTEST_CROSS_REPEAT_MINUTES` (default 60m) — only the first person to post it scores.
- Rapid-fire bursts: two scoring messages less than `CONTEST_MESSAGE_COOLDOWN_SECONDS` apart (default 10s).
- Anything from a bot or a member holding an excluded moderator role.

Every filtered item is counted and reported under **Spam / Duplicates Filtered** on
the leaderboard, so the numbers are always auditable.

Raw activity is stored **unscored**; points are calculated at report time by
replaying events in chronological order. That means the leaderboard is fully
reproducible, and the rules above can be re-tuned in `.env` without ever needing
to re-scan Discord.

---

#### Step 16: Compose and Reuse Embeds (Embed Studio)

1. On the Mod Panel click **📝 Embed Message**. The **Embed Studio** opens privately, showing your current draft, the server's template library and the placeholders you can use.
2. Click **✏️ Edit embed** and fill in whatever you need — every field is optional on its own:
   - **Title**
   - **Message** (the embed body)
   - **Text above the embed** — for a ping or a short intro, e.g. `<@&role>`
   - **Image link** — leaving this blank keeps whatever banner is already set; use **🖼️ Banner image** to upload or remove one
   - **Colour hex** — blank for SafeRest navy, or e.g. `C8A96A`
3. **🖼️ Banner image** sets the big picture inside the embed. **Upload the file straight from your computer** — that is the reliable way — or paste a link if you prefer. To take the banner off, answer `yes` to **Remove the current banner?**.
4. Save it for next time with **💾 Save as template** and give it a name (up to 60 characters). If that name is already taken the bot shows who last changed it and asks before replacing anything.
6. To reuse a template later, pick it from the **📂 Load a saved template** dropdown. It drops straight into the draft, ready to edit or publish. With more than 25 templates the dropdown pages with **◀️ / ▶️**.
7. While a template is loaded the studio tracks whether the draft still matches it:
   - **✅ saved** — the draft and the template are identical.
   - **⚠️ unsaved changes** — click **🔄 Update "name"** to store your edits back into that template.
8. **🗂️ Manage templates** lists everything saved with how often each one was used, when it changed and who changed it. From there you can **📂 Load**, **✏️ Rename** or **🗑️ Delete** (deleting always asks first, and never touches your draft).
9. **👁️ Preview & publish** shows the finished message with every placeholder filled in, then publishes it to the channel you pick. The studio stays open behind the preview.
10. **🧹 New draft** starts a blank message (saved templates are kept), and **✖️ Close** puts the studio away.

Reliability notes:
- **Your draft is auto-saved after every change.** Close the window, let the panel time out or restart the bot — reopening **📝 Embed Message** brings the same draft back, still linked to its template.
- **Templates are per server and shared with the team.** Anyone with **Manage Server** can load, update or delete them, and the library survives restarts because it lives in `data/community.sqlite3`.
- **Names are unique per server, ignoring case.** `Restock` and `restock` are the same template, and extra spaces are tidied away, so the list never fills up with near-duplicates.
- **Every server keeps up to 100 templates.** Delete one you no longer use to make room.
- **The banner is a picture, never a download card.** Whatever you upload or link is saved with the bot and the embed is pointed at that exact file, so it shows inside the card instead of arriving as an attachment underneath it.
- **Links are copied, not trusted.** A pasted link is downloaded once and kept in `data/banners/`, which is why a banner keeps working after the Discord link it came from expires. If the link cannot be fetched the banner is left alone and the reason is shown.
- **Only PNG, JPG, GIF and WebP are accepted**, up to 8 MB. Anything else — SVG, HEIC from an iPhone, AVIF, a PDF, a web page instead of the picture on it — is refused by name, because Discord would show it as a download rather than a picture.
- Bad data is repaired rather than crashing the composer: an image link Discord would reject is dropped with a warning, an over-long body is trimmed, an invalid colour falls back to SafeRest navy, and a template whose stored data cannot be read is flagged with ⚠️ in the list so you can re-save over it.

---

#### Step 16b: Post a Plain Message (Normal Message)

Use this for anything that should read like a normal message instead of a branded card — a quick notice, a photo drop, a short reminder.

1. On the Mod Panel click **💬 Normal Message** and type the message. Placeholders such as `{server_name}` and `{member_count}` work here too, and the box can be left empty if you only want to post pictures.
2. The private composer opens. **The text at the top of that screen is the message** — exactly what the channel will see, with the placeholders already filled in.
3. **✏️ Edit message** reopens the box with your current text still in it. Rewrite it as many times as you like; the preview updates every time.
4. **🖼️ Add images** takes up to **4** pictures. **Upload them straight from your computer** — you can pick several at once — or paste links, one per line, if you prefer.
   - Each picture is fetched and checked as soon as you add it, so a bad one is caught here rather than at publish time.
   - Every picture appears below the composer, showing the exact file the channel will get.
   - **Remove a picture...** appears once you have staged one, and takes several at a time.
5. Pick a channel from **Choose where to publish this message...** and the message goes out.

Reliability notes:
- **Pictures post as pictures, not as downloads.** Each file is re-uploaded under a name built from its actual contents. That matters: Discord decides how to display a file from its extension, so a WebP named `banner.png` — exactly what a `media.discordapp.net` link hands you — used to arrive as a grey download card. Now it does not.
- **Only PNG, JPG, GIF and WebP are accepted**, up to 8 MB each. Anything else is refused by name when you add it.
- **A broken link is caught while you can still fix it.** Nothing is staged that could not be fetched, and the notice names the picture and the reason.
- **A refused publish keeps your work.** Missing permissions or a channel the bot cannot see leaves the draft on screen so you can pick a different channel.
- **`@everyone` and `@here` do not ping from here** — they post as plain text, and the composer says so. Role and member mentions do ping. Use **📣 SafeRest Discord Announcement** for a post that needs to ping the server.
- The composer is private to you and closes itself after 15 minutes of inactivity.

---

#### Step 17: Persistent Controls
- You do **not** need to run `/summon` again when the bot restarts. Every button carries a fixed ID and is re-registered on startup.
- The same is true for the standalone `/onboarding` and `/community-report` panels.
- Clicking **🔄 Refresh Dashboard** on any existing panel refreshes its data and its button set.

---

## Project Structure

```
saferest-bot/
├── .env                  # Private configuration containing bot token (git ignored)
├── .env.example          # Safe template for configuration
├── .gitignore            # Keeps private files out of Git
├── requirements.txt      # Python dependencies
├── start_bot.command     # 1-Click launcher for macOS
├── start_bot.cmd         # 1-Click launcher for Windows
├── config.py             # Global constants, brand theme and palette
├── main.py               # Launcher, persistent view registration, intent fallback
├── core/
│   ├── __init__.py
│   ├── database.py       # Async SQLite store (onboarding, snapshots, reports, settings, templates)
│   ├── embed_script.py   # Placeholder hydration + broadcast message state
│   ├── embed_templates.py # Saved embed template library and auto-saved drafts
│   └── images.py         # Picture checks, real-format filenames, local asset store
├── utils/
│   ├── __init__.py
│   └── embed_builder.py  # Unified embed design system, deltas, ANSI status blocks
├── cogs/
│   ├── __init__.py
│   ├── engagement.py     # Activity tracking, SQLite ledger, ranked reports
│   ├── memberreport.py   # Growth snapshots, join tallies, self-refreshing reports
│   ├── onboarding.py     # Member scanning, welcome posts, warm DMs, onboarding log
│   └── mod_panel.py      # /summon, /onboarding, /test-onboarding, /self-onboarding, /community-report
├── views/
│   ├── __init__.py
│   ├── banner.py                 # Shared banner modal: upload, link or remove
│   ├── broadcast_view.py         # Embed composer and publish preview
│   ├── common.py                 # Permission guards shared by every panel
│   ├── embed_studio_view.py      # Embed Studio: draft, template library, publish
│   ├── engagement_report_view.py # Activity period and custom date controls
│   ├── member_report_view.py     # Day picker, deploy, refresh, manage
│   ├── mod_panel_view.py         # Persistent Mod Panel buttons and embed
│   ├── onboarding_view.py        # Scan, one-screen welcome composer, settings
│   └── plain_message_view.py     # Normal Message: preview, edit, image attachments
├── data/                 # Runtime databases and uploaded pictures (git ignored)
└── README.md             # Documentation and guide
```

### Database Tables

`data/community.sqlite3`

| Table | Purpose |
| --- | --- |
| `onboarded_members` | Who was welcomed, when, by which moderator. |
| `member_snapshots` | One growth baseline per server per UTC day. |
| `member_joins` | Join counts per day and per week. |
| `member_reports` | Deployed live reports and their refresh state. |
| `guild_settings` | Channel choices made from the Mod Panel. |
| `embed_templates` | Saved embed templates, one unique name per server. |
| `embed_drafts` | Each moderator's auto-saved Embed Studio draft. |

`data/engagement.sqlite3`

| Table | Purpose |
| --- | --- |
| `activity_events` | One row per message, reaction or thread, de-duplicated. |
| `engagement_metadata` | When live tracking began. |
| `contest_events` | Raw, unscored contest activity with a content fingerprint and word count. |
| `contest_sessions` | One row per contest round, with its frozen final standings. |

---

## Credits

Created by **Aljay Leodones** for **SafeRest**.

## Member questionnaire and Slack responses

There is no fixed questionnaire channel. Press **📋 Post Questionnaire** in the Mod Panel (or use `/post-questionnaire`), choose the channel and press **📝 Post questionnaire**. The five questions collect TikTok handle, email, phone number, last 30 days TikTok GMV, and an optional birthday. The member's Discord account is captured automatically. Responses are saved locally and sent to the brand's private Slack channel.

You can also open it from **ℹ️ Information Onboarding → Post questionnaire**. Press it again for another copy or pick another channel — every copy works the same and survives restarts.

See [SLACK_ONBOARDING_SETUP.md](SLACK_ONBOARDING_SETUP.md) for admin installation steps, private webhook configuration, connection testing, and delivery troubleshooting.
