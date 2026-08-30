"""curl_cffi sessions pinned to a single proxy (Chrome TLS fingerprint)."""

from __future__ import annotations

import logging
from typing import Any

from curl_cffi import requests as cffi

from .config import (
    AD_ACCESS_PATH,
    AWAIT_PATH,
    DOWNLOAD_PATH,
    DOWNLOAD_PROXY_PATH,
    GENERATE_PATH,
    IMAGE_GEN_ORIGIN,
    PERCHANCE_ORIGIN,
    QUEUE_PATH,
    VERIFY_PATH,
    Settings,
    get_settings,
)
from .models import KeyBundle, ProxyEndpoint
from .parse import as_json, cache_bust, decode_body, extract_ad_access_code, extract_user_key, request_id

log = logging.getLogger("perchance_key.http")


class BoundSession:
    """One proxy, one impersonated TLS session. Never rebind."""

    def __init__(self, proxy: ProxyEndpoint, settings: Settings | None = None, cookies: dict[str, str] | None = None):
        self.settings = settings or get_settings()
        self.proxy = proxy
        self.session = cffi.Session(
            impersonate=self.settings.impersonate,
            timeout=45,
            proxies=proxy.curl_proxies(),
        )
        self.session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        if cookies:
            for k, v in cookies.items():
                self.session.cookies.set(k, v)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def probe_exit_ip(self, timeout: float | None = None) -> str:
        timeout = timeout if timeout is not None else self.settings.proxy_probe_timeout
        r = self.session.get("https://api.ipify.org?format=json", timeout=timeout)
        r.raise_for_status()
        data = r.json()
        ip = str(data.get("ip") or "")
        if not ip:
            raise RuntimeError(f"ipify empty: {r.text[:200]}")
        return ip

    def verify_user(self, thread: int = 0) -> dict[str, Any]:
        url = f"{IMAGE_GEN_ORIGIN}{VERIFY_PATH}"
        r = self.session.get(
            url,
            params={"thread": thread, "__cacheBust": cache_bust()},
            headers={
                "Origin": IMAGE_GEN_ORIGIN,
                "Referer": f"{IMAGE_GEN_ORIGIN}/embed",
            },
        )
        r.raise_for_status()
        body = as_json(r.content) or decode_body(r.content)
        key = extract_user_key(body, str(r.url))
        return {"ok": r.status_code == 200, "body": body, "user_key": key, "status_code": r.status_code}

    def get_ad_access_code(self) -> str | None:
        url = f"{PERCHANCE_ORIGIN}{AD_ACCESS_PATH}"
        r = self.session.get(
            url,
            params={"__cacheBust": cache_bust()},
            headers={
                "Referer": f"{PERCHANCE_ORIGIN}/imageapi",
                "Origin": PERCHANCE_ORIGIN,
            },
        )
        r.raise_for_status()
        return extract_ad_access_code(r.text)

    def generate(
        self,
        bundle: KeyBundle,
        prompt: str,
        *,
        negative_prompt: str = "",
        seed: int = -1,
        resolution: str | None = None,
        guidance_scale: int | None = None,
        channel: str | None = None,
        subchannel: str | None = None,
    ) -> dict[str, Any]:
        rid = request_id()
        ad = bundle.ad_access_code or ""
        payload = {
            "prompt": prompt,
            "negativePrompt": negative_prompt,
            "seed": seed,
            "resolution": resolution or "512x768",
            "guidanceScale": guidance_scale if guidance_scale is not None else 7,
            "channel": channel or self.settings.channel,
            "subChannel": subchannel or self.settings.subchannel,
            "userKey": bundle.user_key,
            "adAccessCode": ad,
            "requestId": rid,
        }
        import json as _json

        url = f"{IMAGE_GEN_ORIGIN}{GENERATE_PATH}"
        r = self.session.post(
            url,
            params={
                "userKey": bundle.user_key,
                "requestId": rid,
                "adAccessCode": ad,
                "__cacheBust": cache_bust(),
            },
            data=_json.dumps(payload, separators=(",", ":")),
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": IMAGE_GEN_ORIGIN,
                "Referer": f"{IMAGE_GEN_ORIGIN}/embed",
            },
        )
        body = as_json(r.content) or decode_body(r.content)
        return {
            "status_code": r.status_code,
            "request_id": rid,
            "body": body,
            "ok": r.status_code == 200,
        }

    def queue_position(self, user_key: str, request_id: str) -> Any:
        r = self.session.get(
            f"{IMAGE_GEN_ORIGIN}{QUEUE_PATH}",
            params={"userKey": user_key, "requestId": request_id},
            headers={"Referer": f"{IMAGE_GEN_ORIGIN}/embed"},
        )
        r.raise_for_status()
        return as_json(r.content) or decode_body(r.content)

    def await_existing(self, user_key: str) -> Any:
        r = self.session.get(
            f"{IMAGE_GEN_ORIGIN}{AWAIT_PATH}",
            params={"userKey": user_key, "__cacheBust": cache_bust()},
            headers={"Referer": f"{IMAGE_GEN_ORIGIN}/embed"},
        )
        r.raise_for_status()
        return as_json(r.content) or decode_body(r.content)

    def download_via_proxy(self, token: str) -> bytes:
        r = self.session.get(
            f"{IMAGE_GEN_ORIGIN}{DOWNLOAD_PROXY_PATH}",
            params={"t": token},
            headers={"Referer": f"{IMAGE_GEN_ORIGIN}/embed"},
        )
        r.raise_for_status()
        return r.content

    def download(self, image_id: str) -> bytes:
        r = self.session.get(
            f"{IMAGE_GEN_ORIGIN}{DOWNLOAD_PATH}",
            params={"imageId": image_id},
            headers={"Referer": f"{IMAGE_GEN_ORIGIN}/embed"},
        )
        r.raise_for_status()
        return r.content
