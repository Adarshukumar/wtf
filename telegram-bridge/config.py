"""Central configuration for the Telegram Bridge.

Everything is driven by environment variables (or a `.env` file loaded via
python-dotenv). The system works with just a `BOT_TOKEN`; every other value
has a sensible default so you can tune it later.

Run `cp .env.example .env` and fill in your token to get started.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional at import time
    pass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _csv_ints(name: str) -> list[int]:
    raw = os.getenv(name, "")
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return out


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


@dataclass
class Settings:
    # --- Telegram ---
    bot_token: str = os.getenv("BOT_TOKEN", "")

    # Comma-separated chat ids allowed to use admin commands (/say, /owners).
    # Empty = anyone who sends /start is treated as an admin.
    admin_ids: list[int] = field(default_factory=lambda: _csv_ints("ADMIN_IDS"))

    # Optional fixed owner chat id. Registered automatically so the server can
    # reach you even before you ever press /start.
    owner_chat_id: int = _int("OWNER_CHAT_ID", 0)

    # --- Relay (local HTTP API the server pushes into) ---
    relay_host: str = os.getenv("RELAY_HOST", "0.0.0.0")
    relay_port: int = _int("RELAY_PORT", 8080)

    # Secret key required to POST /push. If empty, a key is generated once and
    # persisted in data/api_key so it stays stable across restarts.
    api_key: str = os.getenv("API_KEY", "")

    # --- Storage ---
    db_path: Path = Path(os.getenv("DB_PATH", str(DATA_DIR / "state.db")))

    # --- Delivery reliability ---
    send_retries: int = _int("SEND_RETRIES", 5)
    backoff_base: float = _float("BACKOFF_BASE", 2.0)
    max_backoff: float = _float("MAX_BACKOFF", 120.0)
    queue_worker_interval: float = _float("QUEUE_WORKER_INTERVAL", 1.0)

    # --- Heartbeat (periodic "I am alive" reports) ---
    # Seconds between heartbeats, 0 = disabled. Enable per-owner with /hb on.
    heartbeat_interval: int = _int("HEARTBEAT_INTERVAL", 0)

    # --- push.py (client CLI) ---
    # If set, push.py talks to this relay first and falls back to direct
    # Telegram delivery when the relay is unreachable.
    relay_url: str = os.getenv("RELAY_URL", "")

    def relay_endpoint(self) -> str:
        if self.relay_url:
            return self.relay_url.rstrip("/")
        return f"http://127.0.0.1:{self.relay_port}"


def ensure_api_key(settings: Settings) -> str:
    """Return a stable API key, generating + persisting one if not configured."""
    if settings.api_key:
        return settings.api_key

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / "api_key"

    if key_file.exists():
        existing = key_file.read_text(encoding="utf-8").strip()
        if existing:
            settings.api_key = existing
            return existing

    generated = secrets.token_hex(24)
    key_file.write_text(generated, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on non-posix
        pass
    settings.api_key = generated
    return generated


settings = Settings()
