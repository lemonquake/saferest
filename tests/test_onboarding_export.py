"""Spreadsheet export rows rebuilt from saved Slack payloads."""

import unittest

from core.questionnaire import SHEET_COLUMNS, sheet_row, slack_payload


RECORD = {
    "submission_id": "1400000000000000001", "guild_id": 1377025469752410000,
    "user_id": 123456789012345678, "discord_account": "creator",
    "submitted_at": "2026-09-20T12:34:56.789012+00:00",
    "answers": dict(tt_handle="@creator\tname", email="creator@example.com",
                    gmv="=HYPERLINK(\"x\")", birthday="02/29", phone="+1 555 123 4567"),
}


class SheetRowTests(unittest.TestCase):
    def test_row_matches_columns_and_forces_text(self):
        row = sheet_row(slack_payload("SafeRest", RECORD), "sent")
        self.assertEqual(len(row), len(SHEET_COLUMNS))
        self.assertEqual(row[0], "2026-09-20 12:34:56")
        self.assertEqual(row[1], "'@creator name")  # tab would split the column
        self.assertEqual(row[3], "'+1 555 123 4567")  # a leading + is not a formula
        self.assertEqual(row[4], "'=HYPERLINK(\"x\")")  # not run as a formula
        self.assertEqual(row[5], "'02/29")  # not turned into a date
        self.assertEqual(row[7], "'123456789012345678")  # no lost digits
        self.assertEqual(row[-1], "Sent")
        self.assertFalse(any("\t" in cell or "\n" in cell for cell in row))

    def test_missing_birthday_is_blank(self):
        record = {**RECORD, "answers": {**RECORD["answers"], "birthday": ""}}
        self.assertEqual(sheet_row(slack_payload("SafeRest", record), "pending")[5], "")


if __name__ == "__main__":
    unittest.main()
