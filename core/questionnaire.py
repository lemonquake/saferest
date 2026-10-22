"""Questionnaire validation and Slack payloads. No credentials in saved payloads."""

from datetime import datetime
import re
from urllib.parse import urlsplit


def valid_webhook(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == "hooks.slack.com"
        and re.fullmatch(r"/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+", parsed.path)
        and not parsed.query and not parsed.fragment
    )


def validate_answers(values: dict[str, str]) -> dict[str, str]:
    limits = {"tt_handle": 100, "email": 254, "phone": 30}
    answers = {key: values.get(key, "").strip() for key in limits}
    for key, limit in limits.items():
        if len(answers.get(key, "")) > limit:
            raise ValueError("An answer is too long. Please shorten it.")
        if not answers[key]:
            raise ValueError("Please answer all three required questions.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", answers["email"]):
        raise ValueError("Please enter a valid email address, such as name@example.com.")
    # Digits and the usual separators; 7-15 digits covers local and international numbers.
    phone = answers["phone"]
    if not re.fullmatch(r"[0-9 ()+.-]+", phone) or not 7 <= len(re.findall(r"[0-9]", phone)) <= 15:
        raise ValueError("Please enter a valid phone number, such as +1 555 123 4567.")
    return answers


def slack_payload(brand: str, record: dict) -> dict:
    answers = record["answers"]
    fields = [
        ("TikTok handle", answers["tt_handle"]),
        ("Email", answers["email"]),
        ("Phone number", answers["phone"]),
        ("Discord account", record["discord_account"]),
        ("Discord user ID", str(record["user_id"])),
        ("Discord server ID", str(record["guild_id"])),
        ("Submitted (UTC)", record["submitted_at"]),
        ("Submission ID", record["submission_id"]),
    ]
    # Plain text blocks prevent answers from injecting links, mentions or formatting.
    return {
        "text": f"{brand} onboarding response",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{brand} onboarding response"}},
            *[{"type": "section", "text": {"type": "plain_text", "text": f"{label}\n{value}"}}
              for label, value in fields],
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


# Keep the historical export columns so previously saved answers remain available.
SHEET_COLUMNS = [
    "Submitted (UTC)", "TikTok handle", "Email", "Phone number",
    "TikTok GMV — last 30 days", "Birthday (MM/DD)", "Discord account",
    "Discord user ID", "Discord server ID", "Submission ID", "Slack status",
]
SLACK_STATUS = {"sent": "Sent", "pending": "Waiting", "blocked": "Needs attention"}


def sheet_cell(value, as_text: bool = True) -> str:
    """One TSV cell: no tabs/newlines, and a leading apostrophe so Sheets keeps it as typed text."""
    value = " ".join(str(value).split())
    return f"'{value}" if value and as_text else value


def sheet_row(payload: dict, status: str) -> list[str]:
    """Rebuild a spreadsheet row from a saved Slack payload's "label\nvalue" sections."""
    fields = {}
    for block in payload.get("blocks", []):
        if block.get("type") == "section":
            label, _, value = block["text"]["text"].partition("\n")
            fields[label] = value
    if fields.get("Birthday (MM/DD)") == "Not provided":
        fields["Birthday (MM/DD)"] = ""
    try:
        submitted = datetime.fromisoformat(fields.get("Submitted (UTC)", "")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        submitted = fields.get("Submitted (UTC)", "")
    # Answers are forced to text: IDs lose digits, MM/DD becomes a date, and "=..." becomes a formula.
    return [sheet_cell(submitted, as_text=False),
            *[sheet_cell(fields.get(label, "")) for label in SHEET_COLUMNS[1:-1]],
            sheet_cell(SLACK_STATUS.get(status, status), as_text=False)]
