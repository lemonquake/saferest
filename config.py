"""
Configuration settings for the SafeRest Discord Bot.
Created by Aljay Leodones for SafeRest.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _optional_int(name: str, default: str = "") -> int | None:
    """Reads an optional numeric environment variable (blank/invalid -> None)."""
    raw = (os.getenv(name, default) or "").strip()
    return int(raw) if raw.isdigit() else None


def _positive_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


# Discord Bot Token
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Optional Guild ID for testing/instant slash command updates
GUILD_ID = _optional_int("GUILD_ID")

# Engagement reporting
REPORT_CHANNEL_ID = _optional_int("REPORT_CHANNEL_ID")
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Asia/Manila")
ACTIVITY_DATABASE_PATH = Path(
    os.getenv("ACTIVITY_DATABASE_PATH", "data/engagement.sqlite3")
)

# ── Engagement contest ─────────────────────────────────────────────────────
# Only activity inside these channels (and their threads) is ever counted.
# Fill in the SafeRest contest channels before running a contest.
CONTEST_CHANNEL_IDS: set[int] = set()
# Staff and bot roles never score: Admin, Team, Bot.
CONTEST_EXCLUDED_ROLE_IDS: set[int] = {
    1549192877358325941,  # Admin
    1559235558037790790,  # Team
    1559588437068087356,  # Bot
}
CONTEST_REPORT_CHANNEL_ID = _optional_int(
    "CONTEST_REPORT_CHANNEL_ID"
)

# Point weighting
CONTEST_POINTS_MESSAGE = _positive_int("CONTEST_POINTS_MESSAGE", 2)
CONTEST_POINTS_REACTION = _positive_int("CONTEST_POINTS_REACTION", 1)
CONTEST_POINTS_TIKTOK = _positive_int("CONTEST_POINTS_TIKTOK", 10)

# Genuine-message rules. A message only earns points when it clears every one
# of these gates, which is what keeps one-word replies, spam bursts and
# copy-pasted text from farming the leaderboard.
#
# ▸ MIN_WORDS            minimum real words left after links, mentions and
#                        emoji are stripped out ("more than 2 words")
# ▸ SELF_REPEAT_HOURS    the same person repeating the same text inside this
#                        window only scores the first time
# ▸ CROSS_REPEAT_MINUTES identical text copied between different people inside
#                        this window only scores for whoever said it first
# ▸ COOLDOWN_SECONDS     minimum gap between two scoring messages from the
#                        same person (rapid-fire burst guard)
CONTEST_MIN_MESSAGE_WORDS = _positive_int("CONTEST_MIN_MESSAGE_WORDS", 3)
CONTEST_SELF_REPEAT_HOURS = _positive_int("CONTEST_SELF_REPEAT_HOURS", 24)
CONTEST_CROSS_REPEAT_MINUTES = _positive_int("CONTEST_CROSS_REPEAT_MINUTES", 60)
CONTEST_MESSAGE_COOLDOWN_SECONDS = _positive_int(
    "CONTEST_MESSAGE_COOLDOWN_SECONDS", 10
)

# How many past contests the "Past Contests" browser lists at once
CONTEST_HISTORY_LIMIT = _positive_int("CONTEST_HISTORY_LIMIT", 25)

# Community database powering onboarding logs, growth snapshots and live reports
COMMUNITY_DATABASE_PATH = Path(
    os.getenv("COMMUNITY_DATABASE_PATH", "data/community.sqlite3")
)

# Where banner and message pictures are kept once a moderator uploads them.
# They are stored locally on purpose: a Discord image link expires, and a
# saved announcement has to keep its banner long after the link it came from
# stopped working.
BANNER_ASSET_PATH = Path(os.getenv("BANNER_ASSET_PATH", "data/banners"))

# Default onboarding channels. These are only starting values: moderators can
# change them at any time from the Mod Panel (Information Onboarding -> Settings),
# and those saved choices always win over the values below.
WELCOME_CHANNEL_ID = _optional_int("WELCOME_CHANNEL_ID")
ONBOARDING_CHANNEL_ID = _optional_int("ONBOARDING_CHANNEL_ID")
INTRODUCTIONS_CHANNEL_ID = _optional_int("INTRODUCTIONS_CHANNEL_ID")

# Creator onboarding (the TikTok Shop question): where the interactive welcome
# is posted by default, the getting-started channel (#🛍️tiktok-shop-101) that
# learning creators are sent to, and #opportunities-to-earn, linked in the
# message members get once they finish. All can be changed from the Mod Panel
# (Onboarding -> Settings); saved choices win over these values.
CREATOR_WELCOME_CHANNEL_ID = _optional_int("CREATOR_WELCOME_CHANNEL_ID")
CREATOR_GETTING_STARTED_CHANNEL_ID = _optional_int(
    "CREATOR_GETTING_STARTED_CHANNEL_ID"
)
CREATOR_OPPORTUNITIES_CHANNEL_ID = _optional_int(
    "CREATOR_OPPORTUNITIES_CHANNEL_ID", "1560318417922428948"
)

# Member-facing links used in onboarding instructions.
SUPPORT_TICKET_CHANNEL_URL = os.getenv(
    "SUPPORT_TICKET_CHANNEL_URL",
    "https://discord.com/channels/1546233896918720645/1558164502091137024",
)
PRODUCT_INFO_CHANNEL_URL = os.getenv(
    "PRODUCT_INFO_CHANNEL_URL",
    "https://discord.com/channels/1546233896918720645/1557841878064300072",
)

# SafeRest Onboarding: each new member gets a private "<handle>-onboarding"
# channel in this category, and finishing the questionnaire there grants the
# access role that unlocks the rest of the server. Both can be changed from
# the Mod Panel (SafeRest Onboarding -> Settings); saved choices win.
SAFEREST_ONBOARDING_CATEGORY_ID = _optional_int(
    "SAFEREST_ONBOARDING_CATEGORY_ID", "1559588851381575732"
)
SAFEREST_ONBOARDING_ROLE_ID = _optional_int(
    "SAFEREST_ONBOARDING_ROLE_ID", "1559588345598972116"
)  # Member

# SafeRest server roles. Admin and Team can always see every private
# onboarding channel, even if their role has no moderator permissions.
ADMIN_ROLE_ID = 1549192877358325941
TEAM_ROLE_ID = 1559235558037790790
VERIFIED_ROLE_ID = 1559235486361583776
BOT_ROLE_ID = 1559588437068087356
MEMBER_ROLE_ID = 1559588345598972116  # granted after onboarding
STAFF_ROLE_IDS: set[int] = {ADMIN_ROLE_ID, TEAM_ROLE_ID}

# How far back the onboarding scanner looks for members that were never welcomed
ONBOARDING_LOOKBACK_DAYS = _positive_int("ONBOARDING_LOOKBACK_DAYS", 30)

# How often deployed live community reports refresh themselves (hours)
REPORT_REFRESH_HOURS = _positive_int("REPORT_REFRESH_HOURS", 3)

# Presence intent unlocks the "Online now" metric in community reports. It must
# also be enabled in the Discord Developer Portal; the bot automatically falls
# back to running without it when the portal switch is off.
ENABLE_PRESENCE_INTENT = _flag("ENABLE_PRESENCE_INTENT", True)

# Brand theme & aesthetics
BRAND_NAME = "SafeRest"
BOT_NAME = "SafeRest Bot"
BRAND_COLOR = 0x1E5AA8  # Restful Blue
EMBED_FOOTER_TEXT = "SafeRest Management Bot • Created by Aljay Leodones"

# Unified embed palette (see utils/embed_builder.py)
COLOR_BRAND = BRAND_COLOR  # Blue — default brand surface
COLOR_ACCENT = 0x7FC8C2  # Soft seafoam accent
COLOR_SUCCESS = 0x2ECC71  # Emerald Green
COLOR_ERROR = 0xE74C3C  # Crimson Red
COLOR_WARNING = 0xF1C40F  # Amber Yellow
COLOR_INFO = 0x3498DB  # Sapphire Blue

# Member questionnaire: moderators pick the channel each time they post it
# (Mod Panel -> Post Questionnaire). The Slack URL is a private secret.
SLACK_ONBOARDING_WEBHOOK_URL = os.getenv("SLACK_ONBOARDING_WEBHOOK_URL", "").strip()
