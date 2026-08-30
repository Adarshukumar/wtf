"""Local HTTP relay — the door your cloud server knocks on.

Any process on the server (or another machine, with the right API key) can
POST a message here and it will be queued and delivered to your Telegram.
The relay never talks to Telegram itself; it only writes to the durable
outbox. The queue worker handles delivery with retries, so a slow or
unavailable Telegram never causes a failed push — the message just waits.

Endpoints:
    GET  /health   -> liveness probe (no auth)
    GET  /status   -> queue statistics (needs API key)
    POST /push     -> enqueue a message        (needs API key)
"""
from __future__ import annotations

import hmac
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from store import Store


class PushRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)
    chat_id: int | None = None  # omit to reach every registered owner
    parse_mode: str = ""  # "", "HTML", or "MarkdownV2"


def create_app(store: Store, api_key: str, service_name: str = "telegram-bridge") -> FastAPI:
    app = FastAPI(title=service_name, version="1.0.0", docs_url=None, redoc_url=None)

    def _authorized(x_api_key: str | None) -> bool:
        return bool(api_key and x_api_key and hmac.compare_digest(api_key, x_api_key))

    def _require_key(x_api_key: str | None) -> None:
        if not _authorized(x_api_key):
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": service_name,
            "time": time.time(),
            "stats": store.stats(),
        }

    @app.get("/status")
    async def status(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
        _require_key(x_api_key)
        return {"ok": True, "stats": store.stats(), "time": time.time()}

    @app.post("/push")
    async def push(body: PushRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
        _require_key(x_api_key)

        if body.chat_id is not None:
            target_ids = [body.chat_id]
        else:
            target_ids = [o["chat_id"] for o in store.get_owners()]

        if not target_ids:
            raise HTTPException(
                status_code=409,
                detail="no owners registered yet — send /start to the bot first",
            )

        job_ids = store.enqueue_broadcast(body.text, body.parse_mode, target_ids)
        return {
            "ok": True,
            "queued": len(job_ids),
            "job_ids": job_ids,
            "targets": target_ids,
            "time": time.time(),
        }

    @app.get("/")
    async def index(request: Request) -> dict[str, Any]:
        return {
            "service": service_name,
            "ok": True,
            "hint": "private relay — use POST /push with your X-API-Key",
        }

    return app
