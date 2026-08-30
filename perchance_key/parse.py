"""Parse Perchance verifyUser / generate bodies (HAR bodies were often stripped)."""

from __future__ import annotations

import json
import re
from typing import Any

USER_KEY_RE = re.compile(r"\b([a-f0-9]{64})\b", re.I)
USER_KEY_QS_RE = re.compile(r"[?&]userKey=([a-f0-9]{64})", re.I)
AD_CODE_QS_RE = re.compile(r"[?&]adAccessCode=([a-f0-9]{64})", re.I)


def decode_body(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, dict):
        return json.dumps(body)
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", "replace")
    return str(body)


def as_json(body: Any) -> Any:
    if isinstance(body, (dict, list)):
        return body
    text = decode_body(body).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def extract_user_key(body: Any, url: str = "") -> str | None:
    data = as_json(body)
    if isinstance(data, dict):
        for key in ("userKey", "user_key", "key"):
            val = data.get(key)
            if isinstance(val, str) and _looks_like_key(val):
                return val.lower()
        nested = data.get("data")
        if isinstance(nested, dict):
            val = nested.get("userKey") or nested.get("user_key")
            if isinstance(val, str) and _looks_like_key(val):
                return val.lower()
    text = decode_body(body).strip().strip('"')
    if _looks_like_key(text):
        return text.lower()
    m = USER_KEY_QS_RE.search(url or "")
    if m:
        return m.group(1).lower()
    m = USER_KEY_RE.search(text)
    if m:
        return m.group(1).lower()
    if url:
        m = USER_KEY_RE.search(url)
        if m:
            return m.group(1).lower()
    return None


def extract_ad_access_code(body: Any, url: str = "") -> str | None:
    text = decode_body(body).strip().strip('"')
    if _looks_like_key(text):
        return text.lower()
    data = as_json(body)
    if isinstance(data, dict):
        for key in ("adAccessCode", "ad_access_code", "accessCode"):
            val = data.get(key)
            if isinstance(val, str) and _looks_like_key(val):
                return val.lower()
    m = AD_CODE_QS_RE.search(url or "")
    if m:
        return m.group(1).lower()
    m = USER_KEY_RE.search(text)
    if m:
        return m.group(1).lower()
    return None


def _looks_like_key(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{64}", value.strip(), re.I))


def cache_bust() -> str:
    import random

    return str(random.random())


def request_id() -> str:
    import random

    return str(random.random())
