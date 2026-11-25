"""Telegram setup helper: verify the bot token and find chat IDs. Uses no AI calls.

  python scripts/telegram_setup.py               # check token, list chats the bot has seen
  python scripts/telegram_setup.py --test <id>   # send one test message to a chat

Before listing chats: add the bot to your private group (or as an admin of your channel),
then send any message in that group/channel so the bot "sees" it.
Reads TELEGRAM_BOT_TOKEN from the environment or .env. The token is never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

API = "https://api.telegram.org"


def call(token: str, method: str, **payload):
    try:
        resp = httpx.post(f"{API}/bot{token}/{method}", json=payload, timeout=20)
    except httpx.HTTPError as exc:
        sys.exit(f"Network error calling Telegram: {type(exc).__name__}")
    data = resp.json()
    if not data.get("ok"):
        sys.exit(f"Telegram {method} failed: {data.get('description')}")
    return data["result"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", metavar="CHAT_ID", help="send a test message to this chat id")
    args = parser.parse_args()

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (put it in .env)")
    me = call(token, "getMe")
    print(f"Token OK. Bot: @{me.get('username')} ({me.get('first_name')})")

    if args.test:
        call(token, "sendMessage", chat_id=args.test,
             text="✅ Telangana news agent: test message. Telegram delivery is working.")
        print(f"Test message sent to {args.test}.")
        return

    chats = {}
    for upd in call(token, "getUpdates", allowed_updates=["message", "channel_post", "my_chat_member"]):
        for key in ("message", "channel_post", "my_chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    if not chats:
        print("\nNo chats seen yet. Add the bot to your group (or as admin of your channel), send a message\n"
              "there, then run this again. (Updates are kept by Telegram for 24 hours.)")
        return
    print("\nChats the bot has seen (use the ID in TELEGRAM_TEAM_CHAT_IDS / TELEGRAM_REVIEWER_CHAT_IDS):")
    for cid, chat in chats.items():
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        print(f"  {cid:>16}  {chat.get('type'):<10}  {name}")
    print("\nGroup/channel IDs are negative numbers (e.g. -1001234567890); include the minus sign.")


if __name__ == "__main__":
    main()
