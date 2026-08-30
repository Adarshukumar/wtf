"""Durable storage for the bridge: owners + an outbox queue.

The outbox is the reliability core. A message is NEVER sent straight to
Telegram — it is first written to disk (SQLite), and only deleted after
Telegram confirms delivery. If the process dies, the network flaps, or
Telegram is unreachable, every message survives and is retried automatically.

States:
    pending  -> waiting to be sent
    sending  -> picked up by the worker right now
    sent     -> Telegram confirmed delivery (safe to forget)
    failed   -> last attempt failed, will retry with backoff
    dead     -> exhausted all retries, kept for inspection
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    chat_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    subscribed  INTEGER NOT NULL DEFAULT 1,
    heartbeat   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    text            TEXT NOT NULL,
    parse_mode      TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error      TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, next_attempt_at);
"""


class Store:
    def __init__(self, db_path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            import os

            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._recover_stale()

    # ------------------------------------------------------------------ owners
    def register_owner(
        self, chat_id: int, username: str = "", first_name: str = "", heartbeat: bool | None = None
    ) -> None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT heartbeat FROM owners WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO owners (chat_id, username, first_name, subscribed, heartbeat, created_at, last_seen)"
                    " VALUES (?, ?, ?, 1, ?, ?, ?)",
                    (chat_id, username, first_name, int(bool(heartbeat)), now, now),
                )
            else:
                hb = row["heartbeat"] if heartbeat is None else int(bool(heartbeat))
                self._conn.execute(
                    "UPDATE owners SET username = ?, first_name = ?, last_seen = ?, heartbeat = ?"
                    " WHERE chat_id = ?",
                    (username, first_name, now, hb, chat_id),
                )
            self._conn.commit()

    def set_heartbeat(self, chat_id: int, on: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE owners SET heartbeat = ?, last_seen = ? WHERE chat_id = ?",
                (int(on), time.time(), chat_id),
            )
            self._conn.commit()

    def heartbeat_setting(self, chat_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT heartbeat FROM owners WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return bool(row["heartbeat"]) if row else False

    def get_owners(self, subscribed_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM owners"
        if subscribed_only:
            q += " WHERE subscribed = 1"
        q += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(q).fetchall()
        return [dict(r) for r in rows]

    def owner_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM owners WHERE subscribed = 1"
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ outbox
    def enqueue(self, chat_id: int, text: str, parse_mode: str = "") -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outbox (chat_id, text, parse_mode, status, attempts, next_attempt_at, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)",
                (chat_id, text, parse_mode or "", now, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def enqueue_broadcast(self, text: str, parse_mode: str = "", chat_ids: Iterable[int] | None = None) -> list[int]:
        ids = list(chat_ids) if chat_ids is not None else [o["chat_id"] for o in self.get_owners()]
        return [self.enqueue(cid, text, parse_mode) for cid in ids]

    def claim_due(self, now: float, limit: int = 25) -> list[dict[str, Any]]:
        """Mark due pending/failed rows as 'sending' and return them."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, text, parse_mode, attempts FROM outbox"
                " WHERE status IN ('pending','failed') AND next_attempt_at <= ?"
                " ORDER BY next_attempt_at ASC LIMIT ?",
                (now, limit),
            ).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            self._conn.executemany(
                "UPDATE outbox SET status = 'sending', updated_at = ? WHERE id = ?",
                [(now, i) for i in ids],
            )
            self._conn.commit()
        return [dict(r) for r in rows]

    def mark_sent(self, outbox_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET status = 'sent', updated_at = ? WHERE id = ?",
                (time.time(), outbox_id),
            )
            self._conn.commit()

    def mark_failed(self, outbox_id: int, error: str, next_attempt_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET status = 'failed', attempts = attempts + 1,"
                " last_error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (error[:500], next_attempt_at, time.time(), outbox_id),
            )
            self._conn.commit()

    def mark_dead(self, outbox_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET status = 'dead', last_error = ?, updated_at = ? WHERE id = ?",
                (error[:500], time.time(), outbox_id),
            )
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"
            ).fetchall()
        by_status = {r["status"]: int(r["n"]) for r in rows}
        return {
            "owners": self.owner_count(),
            "pending": by_status.get("pending", 0),
            "sending": by_status.get("sending", 0),
            "sent": by_status.get("sent", 0),
            "failed": by_status.get("failed", 0),
            "dead": by_status.get("dead", 0),
        }

    def purge_sent(self, older_than_seconds: float = 86400.0) -> int:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM outbox WHERE status = 'sent' AND updated_at < ?", (cutoff,)
            )
            self._conn.commit()
            return int(cur.rowcount)

    def _recover_stale(self) -> None:
        """Anything left in 'sending' after a crash goes back to 'pending'."""
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET status = 'pending' WHERE status = 'sending'"
            )
            self._conn.commit()
