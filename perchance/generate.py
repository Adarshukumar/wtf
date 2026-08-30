"""curl_cffi image generation on the SAME path the key was minted.

From generator.har / prompt.har:

  POST /api/generate?userKey=&requestId=&adAccessCode=&__cacheBust=
  Content-Type: text/plain;charset=UTF-8
  body: JSON {prompt, negativePrompt, seed, resolution, guidanceScale,
              channel, subChannel, userKey, adAccessCode, requestId}

  success -> {status, imageId, imageDownloadUrl, fileExtension, seed, ...}
  waiting_for_prev_request_to_finish -> GET awaitExistingGenerationRequest, retry
  invalid_ad_access_code -> refresh ad access code
  invalid_key -> key is dead for this IP
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from curl_cffi import requests as cffi

from .parse import as_json, as_text, parse_ad_access
from .proxy import Proxy
from .urls import AD_ACCESS, AWAIT, GENERATE, IMAGE_GEN, IMAGEAPI, PERCHANCE, QUEUE, UA

log = logging.getLogger("perchance.generate")


class GenerateError(RuntimeError):
    pass


class Client:
    def __init__(self, user_key: str, proxy: Proxy | None = None, ad_access_code: str | None = None):
        self.user_key = user_key
        self.proxy = proxy
        self.ad_access_code = ad_access_code or ""
        kwargs: dict[str, Any] = {"impersonate": "chrome131", "timeout": 60}
        if proxy is not None:
            kwargs["proxies"] = proxy.curl_proxies()
        self.s = cffi.Session(**kwargs)
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass

    def _gen_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": IMAGE_GEN,
            "Referer": f"{IMAGE_GEN}/embed",
        }

    def refresh_ad_access(self) -> str:
        r = self.s.get(
            AD_ACCESS,
            params={"__cacheBust": str(int(time.time()))},
            headers={"Referer": IMAGEAPI, "Origin": PERCHANCE},
        )
        r.raise_for_status()
        code = parse_ad_access(r.text) or ""
        self.ad_access_code = code
        return code

    def generate(
        self,
        prompt: str,
        *,
        negative_prompt: str = "",
        seed: int = -1,
        resolution: str = "512x768",
        guidance_scale: int = 7,
        channel: str = "imageapi",
        sub_channel: str = "public",
        retries: int = 8,
    ) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        for attempt in range(1, retries + 1):
            rid = str(random.random())
            body = {
                "prompt": prompt,
                "negativePrompt": negative_prompt,
                "seed": seed,
                "resolution": resolution,
                "guidanceScale": guidance_scale,
                "channel": channel,
                "subChannel": sub_channel,
                "userKey": self.user_key,
                "adAccessCode": self.ad_access_code,
                "requestId": rid,
            }
            r = self.s.post(
                GENERATE,
                params={
                    "userKey": self.user_key,
                    "requestId": rid,
                    "adAccessCode": self.ad_access_code,
                    "__cacheBust": str(random.random()),
                },
                data=json.dumps(body, separators=(",", ":")),
                headers=self._gen_headers(),
            )
            data = as_json(r.content) or {"raw": as_text(r.content), "http": r.status_code}
            status = data.get("status") if isinstance(data, dict) else None
            log.info("generate attempt %d -> %s", attempt, status)
            last = data if isinstance(data, dict) else {"status": "unknown", "body": data}

            if status == "success":
                return last
            if status == "invalid_key":
                raise GenerateError("invalid_key — this userKey is not valid on this IP/proxy")
            if status == "invalid_ad_access_code":
                log.info("refreshing adAccessCode")
                self.refresh_ad_access()
                continue
            if status == "waiting_for_prev_request_to_finish":
                self._await_existing()
                time.sleep(0.5)
                continue
            if status in ("not_logged_in", "failed_verification"):
                raise GenerateError(f"generate refused: {status} (key not verified on this IP)")
            # queue / unknown — peek queue then retry
            try:
                q = self.queue(rid)
                log.info("queue: %s", q)
            except Exception:
                pass
            time.sleep(1.2)
        raise GenerateError(f"generate did not succeed: {last}")

    def queue(self, request_id: str) -> Any:
        r = self.s.get(
            QUEUE,
            params={"userKey": self.user_key, "requestId": request_id},
            headers={"Referer": f"{IMAGE_GEN}/embed"},
        )
        r.raise_for_status()
        return as_json(r.content) or as_text(r.content)

    def _await_existing(self) -> Any:
        r = self.s.get(
            AWAIT,
            params={"userKey": self.user_key, "__cacheBust": str(random.random())},
            headers={"Referer": f"{IMAGE_GEN}/embed"},
            timeout=20,
        )
        return as_json(r.content) or as_text(r.content)

    def download(self, result: dict[str, Any], dest: Path) -> Path:
        rel = result.get("imageDownloadUrl") or ""
        if not rel and result.get("imageId"):
            rel = f"/api/downloadTemporaryImage?imageId={result['imageId']}"
        if not rel:
            raise GenerateError(f"no imageDownloadUrl in {result}")
        url = rel if str(rel).startswith("http") else urljoin(IMAGE_GEN + "/", rel.lstrip("/"))
        r = self.s.get(url, headers={"Referer": f"{IMAGE_GEN}/embed"}, timeout=40)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        log.info("wrote %s (%d bytes)", dest, len(r.content))
        return dest
