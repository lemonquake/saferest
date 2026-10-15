# How to Put the SafeRest Bot in Your Discord Server

This is a step-by-step guide. Each step is small, so just go in order. 🙂

Think of the bot as a **robot helper**. To make it work, you:

1. **Invite** the robot into your server (like inviting a friend to a party).
2. **Give it permission** to do its jobs (like giving it a key to some rooms).
3. **Turn it on** (like pressing the power button on a toy).

---

## Before you start, you need:

- ✅ This Mac computer (the bot lives in the **saferest** folder).
- ✅ The **SafeRest** Discord server, and you need to be an **Admin** there.
- ✅ About 15 minutes.

---

## Step 0: Set up the robot's home page (Discord Developer Portal) 🏠

Every Discord robot has a "home page" where its settings live. You only do this step **once**.

1. Go to **https://discord.com/developers/applications** and log in with your Discord account.
2. Click the box called **SafeRest Bot**.

### 0a. Give it a face and a description *(optional, but nice)*

1. On the left, click **General Information**.
2. Click the round **App Icon** and upload the SafeRest logo.
3. In **Description**, type something like: *The official SafeRest community helper.*
4. Click the green **Save Changes** bar at the bottom.

### 0b. Turn off the "anyone can add me" link

Right now, anybody who finds the robot could add it to *their* server. Let's stop that. Discord
makes you do this part first:

1. On the left, click **Installation**.
2. Under **Installation Contexts**, **untick User Install**. Keep **Guild Install** ticked.
3. Under **Install Link**, choose **None**.
4. Click **Save Changes**.

### 0c. Check the robot's switches

1. On the left, click **Bot**.
2. Turn **Public Bot** **OFF** (gray). Now only you can invite the robot.
3. Make sure **Requires OAuth2 Code Grant** is **OFF** (gray).
4. Scroll down to **Privileged Gateway Intents**. Turn all three **ON** (blue):
   - **Presence Intent**
   - **Server Members Intent**
   - **Message Content Intent**

   *(These are already on. Just double-check them.)*
5. Click **Save Changes**.

> ⚠️ **Don't press "Reset Token"** on this page unless the robot's password got out. The robot
> is already using its current password. If you ever do reset it, open the hidden **.env** file
> in the saferest folder and put the new password after `DISCORD_TOKEN=`.

---

## Step 1: Invite the robot 🤖

1. Click this link (it's the robot's invitation):

   **https://discord.com/oauth2/authorize?client_id=1559227125750562917&permissions=292326468688&scope=bot%20applications.commands**

2. A Discord page opens. It asks **"Add to server"**. Click the box and pick **SafeRest**.
3. Click **Continue**.
4. You will see a list of checkboxes. **Leave them all checked.** These are the jobs the robot is allowed to do.
5. Click **Authorize**.
6. If Discord asks "Are you human?", do the little puzzle.
7. 🎉 The robot, **SafeRest Bot**, is now in your server! It will look gray (asleep) until Step 5.

---

## Step 2: Put the robot's name tag up high 🏷️

In Discord, roles are like name tags, and they are stacked in a list. The robot can only give out name
tags that are **lower than its own**. It needs to give new people the **Member** tag, so its tag
must sit **above Member**.

1. Open the SafeRest server.
2. Click the server name at the top left, then **Server Settings**.
3. Click **Roles**.
4. Find the role called **SafeRest Bot**. Press and hold on it, then **drag it up** so it sits
   **above Member**. (Keep it under **Admin**.)
5. Click **Save Changes** if Discord asks.

**Extra:** If you want the robot to wear your **Bot** role too, go to **Members**, find
**SafeRest Bot**, click the **+** next to its roles and add **Bot**.

---

## Step 3: Give the robot a key to the onboarding room 🔑

The robot makes a little private room for every new person. It builds those rooms inside your
**Onboarding** category, so it needs a key to that category.

1. In your channel list, find the **Onboarding** category (it's the folder-like heading).
2. **Right-click** it, then click **Edit Category**.
3. Click **Permissions**.
4. Click the **+** next to "Roles/Members" and pick **SafeRest Bot**.
5. Turn these to a **green check ✅**:
   - View Channels
   - Manage Channels
   - Manage Permissions
   - Send Messages
   - Read Message History
6. Click **Save Changes**.

---

## Step 4: Hide the fun rooms until people finish onboarding 🚪

New people should only see the onboarding. After they finish, the robot gives them the **Member**
tag and the rest of the server opens up.

For each of your main channels or categories:

1. Right-click it, then **Edit Channel** (or **Edit Category**), then **Permissions**.
2. Click **@everyone** and set **View Channel** to the **red ❌**.
3. Click **+**, add **Member**, and set **View Channel** to the **green ✅**.
4. Click **Save Changes**.

*(If your server is already set up like this, skip this step.)*

---

## Step 5: Turn the robot on ⚡

1. Open **Finder**.
2. Go to your home folder, then **CGK**, then **saferest**.
3. **Double-click** the file called **start_bot.command**.
4. A black window opens and words start scrolling. Wait until you see:

   ```
   Logged in as: SafeRest Bot
   ```

5. 🎉 The robot is awake! In Discord, its dot turns **green**.

> ⚠️ **Important:** the robot is only awake while that black window is open **and** the Mac is on
> and not asleep. If you close the window, the robot goes to sleep. To wake it up again, just
> double-click **start_bot.command** again.

*If the Mac says it can't open the file:* right-click **start_bot.command**, click **Open**,
then click **Open** again.

### Or: turn it on by typing in Terminal ⌨️

This does the same thing as double-clicking. (It's the Mac version of Windows' `start_bot.cmd`.)

1. Open **Terminal**: press **⌘ Command + Space**, type **Terminal**, press **Enter**.
2. Type this and press **Enter**. It walks into the bot's folder:

   ```bash
   cd ~/CGK/saferest
   ```

3. Type this and press **Enter**. It wakes the robot up:

   ```bash
   caffeinate -i ./venv/bin/python main.py
   ```

   `caffeinate -i` keeps the Mac from falling asleep while the robot runs. 😴🚫

4. To turn the robot **off**, click the Terminal window and press **Control + C**.

**Extra (for grown-ups):** to run it in the background so you can close Terminal:

```bash
cd ~/CGK/saferest && nohup caffeinate -i ./venv/bin/python main.py > bot.log 2>&1 &
```

To see what it's doing:

```bash
tail -f ~/CGK/saferest/bot.log
```

To turn the background robot off:

```bash
pkill -f "venv/bin/python main.py"
```

---

## Step 6: Say hi to the robot 👋

1. Go to a staff-only channel in SafeRest (one that new people can't see).
2. Type **/summon** and press **Enter**.
3. The robot posts the **Mod Panel**, a message full of buttons. This is the robot's control
   panel. 🎛️

> If **/summon** doesn't show up, wait 5 minutes and try again, or close Discord and open it again.

---

## Step 7: Check the robot's settings ⚙️

**Onboarding:**

1. On the Mod Panel, click **SafeRest Onboarding**, then **⚙️ Settings**.
2. Make sure it says:
   - **Category:** your Onboarding category
   - **Access role:** **Member**
   - **Verified role:** **Verified**
3. If anything is missing or wrong, pick the right one from its menu.

**Engagement contest:**

1. On the Mod Panel, click **🏆 Engagement Contest**, then **⚙️ Settings**.
2. In the first menu, tick **every channel that should earn points** (up to 25).
3. In the second menu, pick the channel where the **leaderboard** should be posted.
4. Click **✅ Done**. You can come back and change these any time.

**Questionnaire:**

1. On the Mod Panel, click **📋 Post Questionnaire**.
2. Pick the channel you want it in, then press **📝 Post questionnaire**.

**Everything else:** open the other buttons (Onboarding, Information Onboarding, reports) and
pick the channels you want the robot to post in.

---

## Step 8: Test it with a pretend new person 🧪

*Quick check:* on the Mod Panel, open **SafeRest Onboarding** and click **Test on myself**. The
robot shows you what a new person would see.

*Real test:*

1. Ask a friend (or use a second Discord account) to join SafeRest.
2. On the Mod Panel, open **SafeRest Onboarding** and click **🔍 Scan for new members**.
3. Pick today, then press **🎟️ Open onboarding channels**.
4. Your friend gets a private room called **their-name-onboarding**.
5. When they finish the questions, they get the **Member** tag and can see the whole server. ✅

---

## Uh-oh! Something went wrong 🛠️

| What happened | What to do |
| --- | --- |
| The robot's dot is gray | The black window is closed or the Mac is asleep. Double-click **start_bot.command** again. |
| **/summon** isn't there | Wait 5 minutes, then restart Discord. Make sure you're an Admin. |
| The robot can't give the **Member** tag | Go back to **Step 2**. Drag **SafeRest Bot** above **Member**. |
| The robot can't make private rooms | Go back to **Step 3**. Give the robot the key to the Onboarding category. |
| The black window says "Improper token" | The robot's secret password stopped working. Ask the grown-up who made the bot (Aljay) to make a new one. |

---

## 🔒 One big safety rule

The robot has a **secret password** (called a *token*). It's saved in a hidden file named **.env**
in the saferest folder.

**Never** show it to anyone, paste it in Discord or put it in a screenshot. If someone else gets
it, they can control your robot. If that happens, go to the
[Discord Developer Portal](https://discord.com/developers/applications), open **SafeRest Bot**,
click **Bot**, then **Reset Token**, and put the new password into the **.env** file.
