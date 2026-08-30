"""Entrypoint — runs the bot, the relay API and the queue worker together.

One process, one event loop:

    ┌─────────────────────────────────────────────────────────┐
    │  Telegram  ←—long-poll—  aiogram bot  (your control    │
    │                          panel + owner registration)    │
    │                                                         │
    │  your server —HTTP→  FastAPI relay —> outbox (SQLite)   │
    │                                             │           │
    │                                    queue worker (retry) │
    │                                             │           │
    │  Telegram  ←———————————— sendMessage ——————┘           │
    └─────────────────────────────────────────────────────────┘

Run:
    python main.py
"""
from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from bot import build_bot
from config import ensure_api_key, settings
from relay import create_app
from sender import backoff_delay, send_message_async, TelegramError
from store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")


async def queue_worker(store: Store) -> None:
    """Drain the outbox forever, with per-message retry + backoff."""
    log.info("queue worker started")
    while True:
        try:
            import time

            due = store.claim_due(time.time(), limit=25)
            for row in due:
                try:
                    await send_message_async(
                        settings.bot_token,
                        row["chat_id"],
                        row["text"],
                        row["parse_mode"],
                    )
                    store.mark_sent(row["id"])
                    log.info("delivered job %s to %s", row["id"], row["chat_id"])
                except TelegramError as exc:
                    attempts = row["attempts"] + 1
                    if attempts >= settings.send_retries:
                        store.mark_dead(row["id"], str(exc))
                        log.error("job %s GIVEN UP after %s tries: %s", row["id"], attempts, exc)
                    else:
                        delay = backoff_delay(attempts, settings.backoff_base, settings.max_backoff)
                        store.mark_failed(row["id"], str(exc), time.time() + delay)
                        log.warning("job %s retry %s/%s in %.1fs: %s",
                                    row["id"], attempts, settings.send_retries, delay, exc)
                except Exception as exc:  # network/other — retry, never lose the message
                    attempts = row["attempts"] + 1
                    delay = backoff_delay(attempts, settings.backoff_base, settings.max_backoff)
                    store.mark_failed(row["id"], str(exc), time.time() + delay)
                    log.warning("job %s transient error (will retry): %s", row["id"], exc)
        except Exception:
            log.exception("queue worker iteration failed — will keep trying")
        await asyncio.sleep(settings.queue_worker_interval)


async def heartbeat_worker(store: Store) -> None:
    """Optional periodic 'I am alive' reports for owners with /hb on."""
    interval = settings.heartbeat_interval
    if interval <= 0:
        return
    log.info("heartbeat worker started (every %ss)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            targets = [o for o in store.get_owners() if o["heartbeat"]]
            if not targets:
                continue
            import time

            text = f"🫀 <b>Heartbeat</b>\nUptime: <code>{int(time.time())}s since start</code>"
            for o in targets:
                store.enqueue(o["chat_id"], text, "HTML")
        except Exception:
            log.exception("heartbeat error")


async def run_relay(store: Store, api_key: str) -> None:
    app = create_app(store, api_key)
    config = uvicorn.Config(
        app,
        host=settings.relay_host,
        port=settings.relay_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    log.info("relay listening on http://%s:%s", settings.relay_host, settings.relay_port)
    await server.serve()


async def run_bot(store: Store) -> None:
    bot, dp = build_bot(store, settings)
    backoff = 1.0
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            log.info("bot polling started (auto-reconnect armed)")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except Exception as exc:
            log.error("bot polling crashed: %s — restarting in %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
        else:
            backoff = 1.0


async def main() -> None:
    api_key = ensure_api_key(settings)
    if not settings.bot_token:
        log.warning("BOT_TOKEN not set — running RELAY ONLY. "
                    "Pushes are queued and will deliver once a token is configured.")

    store = Store(settings.db_path)

    tasks: list[asyncio.Task] = [
        asyncio.create_task(run_relay(store, api_key)),
        asyncio.create_task(queue_worker(store)),
    ]
    if settings.bot_token:
        tasks.append(asyncio.create_task(run_bot(store)))
    if settings.heartbeat_interval > 0:
        tasks.append(asyncio.create_task(heartbeat_worker(store)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - windows
            pass

    log.info("✅ bridge up — API key: %s (keep this secret)", api_key)
    log.info("   Test push:  curl -X POST http://127.0.0.1:%s/push "
             "-H 'X-API-Key: %s' -H 'Content-Type: application/json' "
             "-d '{\"text\":\"hello from server\"}'", settings.relay_port, api_key)

    await stop.wait()
    log.info("shutting down…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
