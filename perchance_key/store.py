from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Settings, get_settings
from .models import KeyBundle, ProxyEndpoint


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    user_key        TEXT PRIMARY KEY,
    ad_access_code  TEXT,
    proxy           TEXT NOT NULL,
    proxy_url       TEXT NOT NULL,
    proxy_protocol  TEXT,
    country_code    TEXT,
    source          TEXT,
    user_agent      TEXT,
    cookies_json    TEXT,
    extra_json      TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_keys_proxy ON keys(proxy);

CREATE TABLE IF NOT EXISTS dead_proxies (
    proxy       TEXT PRIMARY KEY,
    reason      TEXT,
    seen_at     TEXT NOT NULL
);
"""


class KeyStore:
    def __init__(self, path: Path | None = None, settings: Settings | None = None):
        settings = settings or get_settings()
        settings.ensure_dirs()
        self.path = Path(path or (settings.data_dir / "keys.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def save(self, bundle: KeyBundle) -> None:
        rec = bundle.to_record()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO keys (
                    user_key, ad_access_code, proxy, proxy_url, proxy_protocol,
                    country_code, source, user_agent, cookies_json, extra_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_key) DO UPDATE SET
                    ad_access_code=excluded.ad_access_code,
                    proxy=excluded.proxy,
                    proxy_url=excluded.proxy_url,
                    last_used_at=excluded.created_at
                """,
                (
                    rec["user_key"],
                    rec.get("ad_access_code"),
                    rec["proxy"],
                    rec["proxy_url"],
                    rec.get("proxy_protocol"),
                    rec.get("country_code"),
                    rec.get("source"),
                    rec.get("user_agent"),
                    json.dumps(bundle.cookies or {}),
                    json.dumps(bundle.extra or {}, default=str),
                    _now(),
                ),
            )

    def mark_dead(self, proxy: str, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO dead_proxies(proxy, reason, seen_at) VALUES(?,?,?) "
                "ON CONFLICT(proxy) DO UPDATE SET reason=excluded.reason, seen_at=excluded.seen_at",
                (proxy, reason, _now()),
            )

    def dead(self) -> set[str]:
        rows = self._conn.execute("SELECT proxy FROM dead_proxies").fetchall()
        return {r["proxy"] for r in rows}

    def used_proxies(self) -> set[str]:
        rows = self._conn.execute("SELECT proxy FROM keys").fetchall()
        return {r["proxy"] for r in rows}

    def list_keys(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM keys ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get(self, user_key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM keys WHERE user_key=?", (user_key,)).fetchone()
        return dict(row) if row else None

    def latest(self) -> KeyBundle | None:
        row = self._conn.execute("SELECT * FROM keys ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        return self._row_to_bundle(row)

    def iter_bundles(self) -> Iterator[KeyBundle]:
        for row in self._conn.execute("SELECT * FROM keys ORDER BY created_at DESC"):
            yield self._row_to_bundle(row)

    def _row_to_bundle(self, row: sqlite3.Row) -> KeyBundle:
        proxy_url = row["proxy_url"] or f"http://{row['proxy']}"
        protocol = row["proxy_protocol"] or ("socks5" if proxy_url.startswith("socks5") else "http")
        host, _, port_s = (row["proxy"] or "").rpartition(":")
        cookies = {}
        try:
            cookies = json.loads(row["cookies_json"] or "{}")
        except json.JSONDecodeError:
            cookies = {}
        extra = {}
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except json.JSONDecodeError:
            extra = {}
        return KeyBundle(
            user_key=row["user_key"],
            ad_access_code=row["ad_access_code"],
            proxy=ProxyEndpoint(
                host=host,
                port=int(port_s or 0),
                protocol=protocol,
                country_code=row["country_code"],
            ),
            source=row["source"] or "store",
            user_agent=row["user_agent"] or "",
            cookies=cookies,
            extra=extra,
        )

    def close(self) -> None:
        self._conn.close()
