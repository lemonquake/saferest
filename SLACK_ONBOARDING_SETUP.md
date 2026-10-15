# Connect Discord onboarding to Slack

The code is installed in the SafeRest bot. Slack delivery needs one incoming
webhook for the destination channel, followed by a bot restart.

| Bot | Public Discord questionnaire channel | Private Slack response channel |
| --- | --- | --- |
| SafeRest | any channel a moderator picks | `saferest-discord-data` |

## 1. Have a Slack workspace admin set up the app

Only a workspace admin can install or approve the integration, and they must
belong to the private channel to select it during setup.

1. Open https://api.slack.com/apps while signed into your Slack workspace.
2. Choose **Create an App**, **Blank app** (or **From scratch**), then **Continue**.
3. Name it **Discord Onboarding** and select your workspace. Create the app.
   (If a Discord Onboarding app already exists for another brand, reuse it.)
4. Open **Incoming Webhooks** and enable **Activate Incoming Webhooks**.
5. Click **Add New Webhook to Workspace**.
6. Select **saferest-discord-data** and authorize it. Copy its webhook URL privately.

Each URL is tied to the channel chosen when it was authorized; the bot cannot
override that destination. No Slack history reading scopes are needed. The URL is
a secret: don't put it in Discord, screenshots, source control, or `.env.example`.

Official instructions: https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/

## 2. Save the URL in the bot's private configuration

In `saferest/.env`, replace the blank value:

```dotenv
SLACK_ONBOARDING_WEBHOOK_URL=<webhook for saferest-discord-data>
```

Never reuse another brand's webhook. Replace the placeholder with the complete
HTTPS URL from Slack, without the angle brackets.

## 3. Verify routing, then restart

Run the following from each bot's folder in PowerShell:

```powershell
.\venv\Scripts\python.exe check_slack_connection.py
```

This sends a clearly labelled connection test with no member data. Confirm each
test appears in the matching Slack channel. If a dependency is missing, run:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Stop the existing bot normally with Ctrl+C in its console, then start it using
`start_bot.cmd`. Run only one copy per bot. Environment changes take effect after
restart. The new extension creates its database table automatically.

## 4. Publish the member questionnaire

1. In the Mod Panel, press **📋 Post Questionnaire** (or use `/post-questionnaire`).
   Both require **Manage Server**.
2. Choose the channel from the menu and press **📝 Post questionnaire**. There is no
   fixed channel: you pick it every time, and you can post as many copies in as many
   channels as you like. Every copy works and survives restarts.
3. Members click **Start onboarding**, fill in the form and submit.
4. Four answers are required: TikTok handle, email, last 30 days TikTok GMV, and
   phone number. GMV accepts the member's amount and currency as text. Birthday,
   the last question, is optional, in `MM/DD` format; no birth year is collected.
   The member's Discord account is captured automatically, so it isn't asked.
5. The member gets a private confirmation. Answers are never posted to the public
   Discord channel. The public card explains that the bot saves responses and
   shares them with the team in a private Slack channel.

The bot needs **View Channel**, **Send Messages** and **Embed Links** in each channel
you post the questionnaire to. The member button survives bot
restarts. Members can submit once per server; duplicate submissions do not send
duplicate Slack posts. Moderators can collect corrections separately; this version
does not include a response-editing interface or automatic birthday greetings.

## Delivery and troubleshooting

- `/onboarding-status` privately reports saved, sent, waiting and blocked counts.
- Without a configured webhook, responses remain saved locally and queued.
- The worker checks every 30 seconds. Network/server failures retry with backoff;
  Slack rate limits pause the destination queue for its requested delay.
- Invalid/revoked webhooks and other permanent HTTP errors need attention. Fix
  the `.env` value, restart, then run `/onboarding-status retry_failed:True`.
- Responses and delivery state are in the `questionnaire_responses` table in the
  configured community database (normally `data/community.sqlite3`). Include that
  database in private backups. Logs contain submission IDs and error codes, not
  answers or webhook secrets.
- Delivery is at least once: if Slack accepts a message but the connection drops
  before acknowledging it, a retry can duplicate the Slack message. The stable
  submission ID lets staff identify such duplicates.
- A syntactically configured webhook is not proof of connection. Use the test
  above and check the actual channel. No live Slack delivery has been verified
  until an admin supplies the two webhooks.
