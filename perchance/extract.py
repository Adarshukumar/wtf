"""DrissionPage Chromium — same entry as the HARs, not a hand-built /embed hash.

prompt.har page:

  GET https://perchance.org/imageapi?prompt=a%20cute%20booy
    └─ iframe  https://{hash}.perchance.org/imageapi?…&prompt=…
         └─ iframes  https://image-generation.perchance.org/embed#…
              └─ GET /api/verifyUser
                 GET /api/checkUserVerificationStatus
                 POST /api/generate?userKey=…

userKey lives on the image-generation origin (network + that iframe's
localStorage). Parent origin only has adAccessCode.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from .parse import parse_ad_access, parse_check_status, parse_verify_user
from .proxy import Proxy
from .urls import DEFAULT_PROMPT, UA, imageapi_url

log = logging.getLogger("perchance.extract")

LISTEN_TARGETS = [
    "/api/verifyUser",
    "/api/checkUserVerificationStatus",
    "/api/getAccessCodeForAdPoweredStuff",
    "/api/generate",
]


class ExtractError(RuntimeError):
    pass


def _options(proxy: Proxy | None, headless: bool, user_data: Path):
    from DrissionPage import ChromiumOptions

    co = ChromiumOptions()
    co.set_user_data_path(str(user_data))
    co.set_user_agent(UA)
    co.auto_port()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-blink-features=AutomationControlled")
    # imageapi drops many embed iframes; too-narrow viewports skip verify
    co.set_argument("--window-size=1280,900")
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


_LS_JS = """
const out = {};
try {
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.startsWith('userKey-')) out[k] = localStorage.getItem(k) || '';
  }
  out.__adAccessCode = localStorage.adAccessCode || '';
} catch (e) {}
return out;
"""


def _local_keys(target) -> dict[str, str]:
    try:
        return target.run_js(_LS_JS) or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("localStorage read failed: %s", exc)
        return {}


def _frames(tab) -> list[Any]:
    for getter in (
        lambda: list(tab.get_frames()),
        lambda: list(tab.frames),
        lambda: tab.eles("tag:iframe"),
    ):
        try:
            found = getter()
            if found:
                return list(found)
        except Exception:
            continue
    return []


def _keys_everywhere(tab) -> dict[str, str]:
    """userKey is on the embed iframe origin, not perchance.org."""
    out = {}
    out.update(_local_keys(tab))
    for fr in _frames(tab):
        try:
            extra = _local_keys(fr)
            if extra:
                out.update(extra)
        except Exception:
            continue
    return out


def _wait_packets(tab, seconds: float) -> list[Any]:
    packets: list[Any] = []
    pkt = None
    try:
        pkt = tab.listen.wait(timeout=seconds)
    except TypeError:
        try:
            pkt = tab.listen.wait(seconds)
        except Exception:
            pkt = None
    except Exception:
        pkt = None
    if pkt is not None:
        packets.append(pkt)
        return packets
    try:
        for p in tab.listen.steps(timeout=max(0.2, seconds)):
            packets.append(p)
    except Exception:
        pass
    return packets


def _from_packet(url: str, body: Any) -> tuple[str | None, str | None, str | None]:
    """Return (user_key, verify_status, ad_access_code) from one network row."""
    key = None
    status = None
    ad = None

    if "verifyUser" in url:
        parsed = parse_verify_user(body, url)
        if parsed and parsed.get("verified"):
            return parsed["user_key"], parsed["status"], None
        if parsed:
            log.info("verifyUser pending: %s %s", parsed.get("status"), parsed.get("reason"))
        return None, None, None

    if "checkUserVerificationStatus" in url:
        st = parse_check_status(body)
        log.info("checkUserVerificationStatus=%s", st)
        parsed = parse_verify_user(body, url)
        if parsed and parsed.get("user_key"):
            return parsed["user_key"], "check_verified" if st == "verified" else parsed["status"], None
        if st == "verified":
            parsed = parse_verify_user("", url)
            if parsed and parsed.get("user_key"):
                return parsed["user_key"], "check_verified", None
        return None, None, None

    if "getAccessCodeForAdPoweredStuff" in url:
        ad = parse_ad_access(body)
        if ad:
            log.info("adAccessCode %s…", ad[:12])
        return None, None, ad

    if "/api/generate" in url:
        parsed = parse_verify_user(body, url)
        if parsed and parsed.get("user_key"):
            return parsed["user_key"], "generate_url", None

    return key, status, ad


def extract_user_key(
    proxy: Proxy | None = None,
    *,
    headless: bool = False,
    timeout: float = 120,
    prompt: str = DEFAULT_PROMPT,
) -> dict[str, Any]:
    """Open imageapi in Chrome. Catch userKey from the embed iframe's network."""
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
            tab.listen.start(LISTEN_TARGETS)
        except Exception as exc:  # noqa: BLE001
            log.warning("listen.start targets failed (%s) — listening to all", exc)
            try:
                tab.listen.start()
            except Exception as exc2:  # noqa: BLE001
                log.warning("listen.start: %s", exc2)

        url = imageapi_url(prompt)
        log.info("GET %s", url)
        tab.get(url, timeout=timeout)

        deadline = time.time() + timeout
        user_key = None
        verify_status = None
        ad_code = None

        while time.time() < deadline:
            for p in _wait_packets(tab, 2.0):
                p_url, body = _packet_url_body(p)
                if not p_url and body is None:
                    continue
                k, st, ad = _from_packet(p_url, body)
                if ad and not ad_code:
                    ad_code = ad
                if k:
                    user_key = k
                    verify_status = st
                    log.info("network caught userKey=%s… via %s", user_key[:12], verify_status)
                    break
            if user_key:
                break

            stored = _keys_everywhere(tab)
            if not ad_code:
                ad_code = stored.get("__adAccessCode") or ad_code
            for k, v in stored.items():
                if k.startswith("userKey-") and v and len(v) >= 64:
                    user_key = v.lower()
                    verify_status = verify_status or "iframe_localStorage"
                    log.info("iframe localStorage %s=%s…", k, user_key[:12])
                    break
            if user_key:
                break

        stored = _keys_everywhere(tab)
        if not user_key:
            for k, v in stored.items():
                if k.startswith("userKey-") and v:
                    user_key = v.lower()
                    verify_status = "iframe_localStorage"
        if not ad_code:
            ad_code = stored.get("__adAccessCode") or None

        try:
            tab.listen.stop()
        except Exception:
            pass

        if not user_key:
            raise ExtractError(
                "no userKey on imageapi network (verifyUser never reached "
                "success/already_verified). Turnstile blocked, iframe never "
                "booted, or proxy dead. Use headed Chrome / xvfb-run, not --headless."
            )

        return {
            "user_key": user_key,
            "status": verify_status,
            "ad_access_code": ad_code or None,
            "proxy": proxy.url if proxy else None,
            "page": url,
            "via": (
                "network_log"
                if verify_status
                in ("success", "already_verified", "check_verified", "generate_url")
                else verify_status
            ),
        }
    finally:
        try:
            if browser is not None:
                browser.quit()
        except Exception:
            pass
        tmp.cleanup()
