"""Smoke tests that run WITHOUT a bot token (no network needed)."""
from __future__ import annotations

import time

from config import Settings
from store import Store


def test_owner_and_outbox_roundtrip(tmp_path):
    store = Store(tmp_path / "state.db")

    store.register_owner(111, username="u", first_name="F")
    assert store.owner_count() == 1
    assert store.heartbeat_setting(111) is False

    store.set_heartbeat(111, True)
    assert store.heartbeat_setting(111) is True

    jid = store.enqueue(111, "hello")
    due = store.claim_due(time.time() + 1)
    assert len(due) == 1 and due[0]["id"] == jid

    store.mark_sent(jid)
    assert store.stats()["sent"] == 1
    assert store.claim_due(time.time() + 1) == []


def test_failed_retry_and_dead(tmp_path):
    store = Store(tmp_path / "state.db")
    store.register_owner(222)
    jid = store.enqueue(222, "retry me")

    store.mark_failed(jid, "boom", time.time())
    store.mark_failed(jid, "boom", time.time())
    store.mark_dead(jid, "given up")

    stats = store.stats()
    assert stats["dead"] == 1
    assert stats["failed"] == 0


def test_crash_recovery_requeues_sending(tmp_path):
    store = Store(tmp_path / "state.db")
    store.register_owner(333)
    jid = store.enqueue(333, "crash safety")
    store.claim_due(time.time() + 1)  # -> status 'sending'

    # Simulate a fresh process restarting on the same DB.
    store2 = Store(tmp_path / "state.db")
    assert store2.claim_due(time.time() + 1)  # recovered to pending and claimable


def test_config_defaults():
    s = Settings()
    assert s.relay_port > 0
    assert s.send_retries > 0
