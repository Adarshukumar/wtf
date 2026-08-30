#!/usr/bin/env python3
"""Send a message to your Telegram from anywhere on the server.

Usage (from your scripts, cron, monitoring, backups, CI …):

    python push.py "CPU is at 99% 🔥"
    python push.py "deploy finished" --parse-mode HTML
    python push.py --file /tmp/report.txt
    python push.py "hi" --chat-id 123456789

Or import it:

    from push import notify
    notify("backup completed ✅")

Delivery strategy (automatic failover):
    1. If the local relay is reachable -> POST /push (queued, durable).
    2. Otherwise -> send straight to Telegram with retries + backoff.
The message is only reported as failed if BOTH paths are exhausted.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from config import ensure_api_key, settings
from sender import TelegramError, backoff_delay, send_message_sync
from store import Store


def _target_ids(chat_id: int | None) -> list[int]:
    if chat_id:
        return [chat_id]
    if settings.owner_chat_id:
        return [settings.owner_chat_id]
    # Fall back to owners registered in the local store (same machine as bot).
    try:
        store = Store(settings.db_path)
        ids = [o["chat_id"] for o in store.get_owners()]
        if ids:
            return ids
    except Exception:
        pass
    return []


def _via_relay(text: str, chat_id: int | None, parse_mode: str) -> bool:
    key = ensure_api_key(settings)
    payload: dict = {"text": text}
    if chat_id:
        payload["chat_id"] = chat_id
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{settings.relay_endpoint()}/push",
                json=payload,
                headers={"X-API-Key": key},
            )
        if resp.status_code == 200:
            print(f"✅ queued via relay: {resp.json()['queued']} message(s)")
            return True
        print(f"⚠️  relay answered {resp.status_code}: {resp.text.strip()}", file=sys.stderr)
    except Exception as exc:
        print(f"⚠️  relay unreachable ({exc}) — falling back to direct send", file=sys.stderr)
    return False


def _direct(text: str, chat_ids: list[int], parse_mode: str) -> bool:
    if not settings.bot_token:
        print("❌ BOT_TOKEN not set — nothing to send with.", file=sys.stderr)
        return False
    ok = True
    for cid in chat_ids:
        sent = False
        for attempt in range(1, settings.send_retries + 1):
            try:
                send_message_sync(settings.bot_token, cid, text, parse_mode)
                print(f"✅ sent directly to {cid}")
                sent = True
                break
            except Exception as exc:
                delay = backoff_delay(attempt, settings.backoff_base, settings.max_backoff)
                print(f"⚠️  attempt {attempt}/{settings.send_retries} to {cid} failed "
                      f"({exc}) — retrying in {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
        if not sent:
            print(f"❌ failed to deliver to {cid} after {settings.send_retries} tries", file=sys.stderr)
            ok = False
    return ok


def notify(text: str, chat_id: int | None = None, parse_mode: str = "") -> bool:
    """Public API. Returns True if the message was accepted for delivery."""
    if not text or not text.strip():
        print("❌ empty message", file=sys.stderr)
        return False
    text = text.strip()
    if _via_relay(text, chat_id, parse_mode):
        return True
    ids = _target_ids(chat_id)
    if not ids:
        print("❌ no destination — set OWNER_CHAT_ID, pass --chat-id, or run "
              "/start once so the bot knows your chat id.", file=sys.stderr)
        return False
    return _direct(text, ids, parse_mode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Push a message to your Telegram")
    parser.add_argument("text", nargs="?", help="message text")
    parser.add_argument("--file", help="read message from a file")
    parser.add_argument("--chat-id", type=int, default=None, help="override destination")
    parser.add_argument("--parse-mode", default="", help="HTML or MarkdownV2")
    args = parser.parse_args()

    text = args.text
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    if not text:
        parser.error("provide a message or --file")

    return 0 if notify(text, args.chat_id, args.parse_mode) else 1


if __name__ == "__main__":
    sys.exit(main())
