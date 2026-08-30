"""DrissionPage Chromium: load /embed, listen until verifyUser says verified.

The embed JS (generator.har):

  1. GET /api/verifyUser?thread=0
     -> failed_verification / token_required   (ignore these)
  2. Cloudflare Turnstile widget (sitekey 0x4AAAAAAAA8g8NphwaSOT59)
  3. GET /api/verifyUser?token=<turnstile>&thread=0
     -> {"status":"success","userKey":"..."}
  4. later: {"status":"already_verified","userKey":"..."}
  5. stores localStorage[`userKey-${thread}`]

We do **not** generate in Chrome. We only wait for the verified packet,
read the key, and close the browser. Image gen is curl_cffi.

A non-empty hash prompt is required — without it embed bails with
"No prompt provided" and never calls verifyUser.
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .parse import parse_check_status, parse_verify_user
from .proxy import Proxy
from .urls import EMBED, UA, VERIFY_USER

log = logging.getLogger("perchance.extract")


class ExtractError(RuntimeError):
    pass


def embed_url(prompt: str = ".") -> str:
    payload = {
        "prompt": prompt or ".",
        "seed": -1,
        "resolution": "512x768",
        "guidanceScale": 7,
        "negativePrompt": "",
        "saveChannel": "imageapi",
        "requestId": str(random.random()),
    }
    return EMBED + "#" + quote(json.dumps(payload, separators=(",", ":")))


def _options(proxy: Proxy | None, headless: bool, user_data: Path):
    from DrissionPage import ChromiumOptions

    co = ChromiumOptions()
    co.set_user_data_path(str(user_data))
    co.set_user_agent(UA)
    co.auto_port()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--window-size=1000,800")
    if proxy is not None:
        co.set_proxy(proxy.url)
        log.info("chrome proxy %s", proxy.url)
    else:
        log.info("chrome DIRECT (no proxy)")
    if headless:
        try:
            co.headless(True)
        except TypeError:
            co.set_argument("--headless=new")
    for cand in (
        os.environ.get("CHROME_PATH"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ):
        if cand and Path(cand).exists():
            co.set_browser_path(cand)
            break
    return co


def _packet_url_body(pkt: Any) -> tuple[str, Any]:
    url = getattr(pkt, "url", None) or ""
    req = getattr(pkt, "request", None)
    if not url and req is not None:
        url = getattr(req, "url", "") or ""
    resp = getattr(pkt, "response", None)
    body = None
    if resp is not None:
        body = getattr(resp, "body", None)
    return str(url or ""), body


def _local_keys(tab) -> dict[str, str]:
    try:
        return tab.run_js(
            """
            const out = {};
            for (let i = 0; i < localStorage.length; i++) {
              const k = localStorage.key(i);
              if (k && k.startsWith('userKey-')) out[k] = localStorage.getItem(k) || '';
            }
            out.__adAccessCode = localStorage.adAccessCode || '';
            return out;
            """
        ) or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("localStorage read failed: %s", exc)
        return {}


def extract_user_key(
    proxy: Proxy | None = None,
    *,
    headless: bool = False,
    timeout: float = 120,
    prompt: str = ".",
) -> dict[str, Any]:
    """Boot one Chrome (optionally proxied). Return when network says verified."""
    try:
        from DrissionPage import Chromium
    except ImportError as exc:
        raise ExtractError("DrissionPage missing — pip install -r requirements.txt") from exc

    tmp = tempfile.TemporaryDirectory(prefix="perc-chrome-")
    browser = None
    try:
        co = _options(proxy, headless, Path(tmp.name))
        browser = Chromium(co)
        tab = browser.latest_tab or browser.new_tab()

        try:
            tab.listen.start(["/api/verifyUser", "/api/checkUserVerificationStatus"])
        except Exception as exc:  # noqa: BLE001
            log.warning("listen.start: %s", exc)

        url = embed_url(prompt)
        log.info("GET %s", url.split("#")[0] + "#…")
        tab.get(url, timeout=timeout)

        deadline = time.time() + timeout
        last_pending = None
        user_key = None
        verify_status = None

        while time.time() < deadline:
            pkt = None
            try:
                pkt = tab.listen.wait(timeout=2)
            except TypeError:
                try:
                    pkt = tab.listen.wait(2)
                except Exception:
                    pkt = None
            except Exception:
                pkt = None

            packets = [pkt] if pkt is not None else []
            if not packets:
                try:
                    for p in tab.listen.steps(timeout=1):
                        packets.append(p)
                except Exception:
                    pass

            for p in packets:
                if p is None:
                    continue
                p_url, body = _packet_url_body(p)
                if "verifyUser" in p_url:
                    parsed = parse_verify_user(body, p_url)
                    if parsed and parsed.get("verified"):
                        user_key = parsed["user_key"]
                        verify_status = parsed["status"]
                        log.info("network verified (%s) userKey=%s…", verify_status, user_key[:12])
                        break
                    if parsed:
                        last_pending = parsed
                        log.info("verifyUser pending: %s %s", parsed.get("status"), parsed.get("reason"))
                elif "checkUserVerificationStatus" in p_url:
                    st = parse_check_status(body)
                    log.info("checkUserVerificationStatus=%s", st)
                    if st == "verified":
                        stored = _local_keys(tab)
                        for k, v in stored.items():
                            if k.startswith("userKey-") and v:
                                user_key = v
                                verify_status = "check_verified"
                                break
            if user_key:
                break

            stored = _local_keys(tab)
            for k, v in stored.items():
                if k.startswith("userKey-") and v and len(v) == 64:
                    # only accept if network already confirmed, or we saw already_verified
                    # localStorage is set at the same moment as success — good enough
                    # but only after we have been waiting a bit (turnstile)
                    if last_pending is None or last_pending.get("verified") is False:
                        # still wait for network unless the page already stored a key
                        # after a success the JS writes localStorage immediately
                        pass
                    user_key = v.lower()
                    verify_status = verify_status or "localStorage"
                    log.info("localStorage %s=%s…", k, user_key[:12])
                    break
            if user_key:
                break

        stored = _local_keys(tab)
        if not user_key:
            for k, v in stored.items():
                if k.startswith("userKey-") and v:
                    user_key = v.lower()
                    verify_status = "localStorage"

        try:
            tab.listen.stop()
        except Exception:
            pass

        if not user_key:
            raise ExtractError(
                "Chrome never got a verified userKey "
                f"(last pending={last_pending}). Turnstile blocked or proxy dead. "
                "Run headed (default) under Xvfb, not --headless."
            )

        ad = stored.get("__adAccessCode") or None
        return {
            "user_key": user_key,
            "status": verify_status,
            "ad_access_code": ad or None,
            "proxy": proxy.url if proxy else None,
            "via": "network_log" if verify_status in ("success", "already_verified", "check_verified") else verify_status,
        }
    finally:
        try:
            if browser is not None:
                browser.quit()
        except Exception:
            pass
        tmp.cleanup()
