"""Export saved onboarding responses as a TSV file to copy-paste into Google Sheets."""

from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import sys

from config import BRAND_NAME, COMMUNITY_DATABASE_PATH
from core.questionnaire import SHEET_COLUMNS, sheet_row


def main():
    database = Path(COMMUNITY_DATABASE_PATH).resolve()
    if not database.exists():
        print(f"No database found at {database}.")
        return 1
    # Read-only, so this is safe while the bot is running.
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        try:
            rows = connection.execute(
                "SELECT payload, status FROM questionnaire_responses ORDER BY rowid"
            ).fetchall()
        except sqlite3.OperationalError:
            print("No questionnaire responses have been saved yet.")
            return 1
    lines = [SHEET_COLUMNS] + [sheet_row(json.loads(payload), status) for payload, status in rows]
    slug = re.sub(r"[^a-z0-9]+", "-", BRAND_NAME.lower()).strip("-")
    # data/ is git-ignored; the export holds member emails, birthdays and phone numbers.
    output = Path("data/exports") / f"{slug}-onboarding-{datetime.now():%Y%m%d-%H%M%S}.tsv"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join("\t".join(line) for line in lines) + "\n", encoding="utf-8")
    print(f"Exported {len(rows)} response(s) to {output.resolve()}")
    print("Open it in Notepad, press Ctrl+A then Ctrl+C, click cell A1 in Google Sheets and press Ctrl+V.")
    print("The file contains member emails, birthdays and phone numbers. Delete it once it's in the Sheet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
