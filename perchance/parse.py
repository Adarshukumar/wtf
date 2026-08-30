"""Turn verifyUser / generate bodies into structured facts.

Observed in generator.har:

  GET /api/verifyUser?thread=0
      {"status":"failed_verification","reason":"token_required"}   # 15 times
  GET /api/verifyUser?token=1.<turnstile>&thread=0
      {"status":"success","userKey":"<64 hex>"}
  GET /api/verifyUser?thread=0
      {"status":"already_verified","userKey":"<64 hex>"}

prompt.har (later session, already verified):

  GET /api/checkUserVerificationStatus?userKey=<64 hex>
      {"status":"verified"}   # 21 bytes
"""

from __future__ import annotations

import json
import re
from typing import Any

USER_KEY_RE = re.compile(r"^[a-f0-9]{64}$", re.I)
USER_KEY_IN_TEXT = re.compile(r"\b([a-f0-9]{64})\b", re.I)

VERIFIED_STATUSES = frozenset({"success", "already_verified"})
PENDING_STATUSES = frozenset({"failed_verification", "token_required", "fetch_failure"})


def as_text(body: Any) -> str:
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
    text = as_text(body).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def looks_like_key(value: str | None) -> bool:
    return bool(value) and bool(USER_KEY_RE.fullmatch(value.strip()))


def parse_verify_user(body: Any, url: str = "") -> dict[str, Any] | None:
    """Return a dict if this packet is a *verified* userKey. None if still pending."""
    data = as_json(body)
    if isinstance(data, dict):
        status = str(data.get("status") or "")
        key = data.get("userKey") or data.get("user_key")
        if status in VERIFIED_STATUSES and looks_like_key(str(key or "")):
            return {
                "verified": True,
                "status": status,
                "user_key": str(key).lower(),
                "reason": data.get("reason"),
            }
        if status == "failed_verification" or data.get("reason") == "token_required":
            return {
                "verified": False,
                "status": status or "failed_verification",
                "reason": data.get("reason"),
                "user_key": None,
            }
    # query string fallback (generate URLs also carry userKey)
    if url:
        m = re.search(r"[?&]userKey=([a-f0-9]{64})", url, re.I)
        if m:
            return {"verified": True, "status": "from_url", "user_key": m.group(1).lower()}
    text = as_text(body).strip().strip('"')
    if looks_like_key(text):
        return {"verified": True, "status": "plain", "user_key": text.lower()}
    return None


def parse_check_status(body: Any) -> str | None:
    data = as_json(body)
    if isinstance(data, dict):
        return str(data.get("status") or "")
    text = as_text(body).strip()
    if text == '{"status":"verified"}':
        return "verified"
    return text or None


def parse_ad_access(body: Any) -> str | None:
    text = as_text(body).strip().strip('"')
    if looks_like_key(text):
        return text.lower()
    data = as_json(body)
    if isinstance(data, dict):
        for k in ("adAccessCode", "accessCode", "code"):
            if looks_like_key(str(data.get(k) or "")):
                return str(data.get(k)).lower()
    return None
