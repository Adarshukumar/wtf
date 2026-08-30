"""Low-level Telegram delivery via the Bot API.

Both the in-process queue worker (async) and the standalone push.py client
(sync) use the exact same HTTP path so behaviour is identical everywhere.
"""
from __future__ import annotations

import time

import httpx

SEND_MESSAGE_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 30.0


class TelegramError(Exception):
    """Raised when Telegram rejects a delivery."""


def _build_payload(chat_id: int, text: str, parse_mode: str = "") -> dict:
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return payload


def _raise_for(data: dict, status_code: int) -> dict:
    if status_code != 200 or not data.get("ok"):
        raise TelegramError(
            f"status={status_code} description={data.get('description', 'unknown')}"
        )
    return data


async def send_message_async(
    token: str, chat_id: int, text: str, parse_mode: str = ""
) -> dict:
    url = SEND_MESSAGE_URL.format(token=token)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(url, json=_build_payload(chat_id, text, parse_mode))
        return _raise_for(resp.json(), resp.status_code)


def send_message_sync(token: str, chat_id: int, text: str, parse_mode: str = "") -> dict:
    url = SEND_MESSAGE_URL.format(token=token)
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.post(url, json=_build_payload(chat_id, text, parse_mode))
        return _raise_for(resp.json(), resp.status_code)


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff: base * 2**attempt, capped. Plus small jitter."""
    delay = min(base * (2 ** max(attempt - 1, 0)), cap)
    jitter = (time.time() % 1.0) * 0.3
    return delay + jitter
