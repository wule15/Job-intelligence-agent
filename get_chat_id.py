"""
Print your Telegram chat id.

Message your bot once, then run this. Put the value it prints into .env
as TELEGRAM_CHAT_ID.
"""

import sys

import requests

from config import Config


def main():
    token = Config.TELEGRAM_BOT_TOKEN
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. Add it to .env and try again.")
        return 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"

    try:
        response = requests.get(url, timeout=10)
    except requests.exceptions.RequestException as e:
        # Print the exception class only. requests puts the full request URL
        # into connection errors, and that URL contains the bot token.
        print(f"Could not reach the Telegram API: {type(e).__name__}")
        return 1

    if response.status_code != 200:
        print(f"Telegram returned HTTP {response.status_code}.")
        return 1

    results = response.json().get("result", [])
    if not results:
        print("No updates. Send a message to your bot, then run this again.")
        return 1

    for update in results:
        chat = update.get("message", {}).get("chat", {})
        if chat.get("id"):
            print(f"chat id: {chat['id']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
