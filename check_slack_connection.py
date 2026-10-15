"""Send a clearly labelled connection test, containing no member answers."""

import asyncio
import sys

import aiohttp

from config import BRAND_NAME, SLACK_ONBOARDING_WEBHOOK_URL
from core.questionnaire import valid_webhook


async def main():
    if not valid_webhook(SLACK_ONBOARDING_WEBHOOK_URL):
        print("Set SLACK_ONBOARDING_WEBHOOK_URL in this bot's private .env file first.")
        return 1
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(
                SLACK_ONBOARDING_WEBHOOK_URL,
                json={"text": f"Connection test: {BRAND_NAME} Discord onboarding can send responses to this channel. This is a setup test, not a member submission."},
                allow_redirects=False,
            ) as response:
                if response.status == 200 and (await response.text()).strip() == "ok":
                    print("Slack accepted the test. Check that it appeared in the correct brand channel.")
                    return 0
                print(f"Slack rejected the test (HTTP {response.status}). Check the app and webhook configuration.")
                return 1
    except (aiohttp.ClientError, asyncio.TimeoutError):
        print("Could not reach Slack. Check the connection and try again.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
