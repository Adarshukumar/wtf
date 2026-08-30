"""DrissionPage Chromium bound to a single proxy.

Why Chromium: Perchance sits behind Cloudflare (cdn-cgi/challenge-platform
in the HAR). verifyUser only returns a real userKey from a browser that
already loaded /embed on that IP.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .config import (
    AD_ACCESS_PATH,
    EMBED_URL,
    IMAGEAPI_URL,
    IMAGE_GEN_ORIGIN,
    PERCHANCE_ORIGIN,
    VERIFY_PATH,
    Settings,
    get_settings,
)
from .models import ProxyEndpoint
from .parse import decode_body, extract_ad_access_code, extract_user_key

log = logging.getLogger("perchance_key.browser")


VERIFY_JS = r"""
return (async () => {
  const url = '/api/verifyUser?thread=0&__cacheBust=' + Math.random();
  const res = await fetch(url, { credentials: 'include' });
  const text = await res.text();
  return { status: res.status, text: text };
})();
"""

AD_ACCESS_JS = r"""
return (async () => {
  const url = '/api/getAccessCodeForAdPoweredStuff?__cacheBust=' + Math.random();
  const res = await fetch(url, { credentials: 'include' });
  const text = await res.text();
  return { status: res.status, text: text };
})();
"""


class BrowserError(RuntimeError):
    pass


def _chromium_options(proxy: ProxyEndpoint, settings: Settings, user_data: Path):
    try:
        from DrissionPage import ChromiumOptions
    except ImportError as exc:
        raise BrowserError(
            "DrissionPage is not installed. pip install -r requirements.txt"
        ) from exc

    co = ChromiumOptions()
    co.set_user_data_path(str(user_data))
    co.set_user_agent(settings.user_agent)
    co.set_proxy(proxy.chromium_proxy)
    co.auto_port()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--lang=en-US")
    if settings.headless:
        try:
            co.headless(True)
        except TypeError:
            co.set_argument("--headless=new")
    # Prefer a real Chrome/Chromium if the environment has one.
    for candidate in (
        os.environ.get("CHROME_PATH"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if candidate and Path(candidate).exists():
            co.set_browser_path(candidate)
            break
    return co


class ProxiedChrome:
    """Own a Chromium process for the lifetime of one proxy."""

    def __init__(self, proxy: ProxyEndpoint, settings: Settings | None = None):
        self.proxy = proxy
        self.settings = settings or get_settings()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="perc-chrome-", dir=str(self.settings.data_dir / ".chrome"))
        self.browser = None
        self.tab = None

    def start(self) -> None:
        from DrissionPage import Chromium

        co = _chromium_options(self.proxy, self.settings, Path(self._tmpdir.name))
        log.info("starting chromium via %s", self.proxy.chromium_proxy)
        self.browser = Chromium(co)
        self.tab = self.browser.latest_tab
        if self.tab is None:
            self.tab = self.browser.new_tab()

    def close(self) -> None:
        try:
            if self.browser is not None:
                self.browser.quit()
        except Exception as exc:  # noqa: BLE001
            log.debug("browser quit: %s", exc)
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass

    def __enter__(self) -> "ProxiedChrome":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def cookies_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            cookies = self.tab.cookies() if self.tab is not None else []
        except Exception:
            cookies = []
        for c in cookies or []:
            if isinstance(c, dict) and c.get("name"):
                out[str(c["name"])] = str(c.get("value") or "")
            elif hasattr(c, "name"):
                out[str(c.name)] = str(getattr(c, "value", "") or "")
        return out

    def _listen_start(self, targets: list[str]) -> None:
        try:
            self.tab.listen.start(targets)
        except Exception as exc:  # noqa: BLE001
            log.debug("listen.start failed: %s", exc)

    def _listen_stop(self) -> None:
        try:
            self.tab.listen.stop()
        except Exception:
            pass

    def _drain_listen(self, timeout: float) -> list[Any]:
        packets = []
        deadline = time.time() + timeout
        listen = getattr(self.tab, "listen", None)
        if listen is None:
            return packets
        while time.time() < deadline:
            remaining = max(0.2, deadline - time.time())
            pkt = None
            try:
                pkt = listen.wait(timeout=min(4.0, remaining))
            except TypeError:
                try:
                    pkt = listen.wait(min(4.0, remaining))
                except Exception:
                    pkt = None
            except Exception:
                pkt = None
            if pkt is None:
                # some versions use steps()
                try:
                    for p in listen.steps(timeout=1):
                        packets.append(p)
                    break
                except Exception:
                    break
            packets.append(pkt)
        return packets

    def mint_user_key(self, on_packet: Callable[[str, Any], None] | None = None) -> tuple[str | None, str | None, Any]:
        """Load /embed through the proxy and pull userKey from verifyUser.

        Returns (user_key, ad_access_code, raw_verify_body).
        """
        if self.tab is None:
            raise BrowserError("browser not started")

        user_key: str | None = None
        ad_code: str | None = None
        raw_verify: Any = None

        self._listen_start(
            [
                f"{IMAGE_GEN_ORIGIN}{VERIFY_PATH}",
                VERIFY_PATH,
                "verifyUser",
                f"{PERCHANCE_ORIGIN}{AD_ACCESS_PATH}",
                "getAccessCodeForAdPoweredStuff",
            ]
        )
        timeout = self.settings.browser_timeout
        log.info("GET %s", EMBED_URL)
        ok = self.tab.get(EMBED_URL, timeout=timeout)
        if ok is False:
            log.warning("embed navigation returned False — continuing anyway")

        # Give Cloudflare + embed JS a moment, then drain listen + JS fetch.
        time.sleep(2.5)
        for pkt in self._drain_listen(8):
            url, body = _packet_url_body(pkt)
            if on_packet:
                on_packet(url, body)
            if "verifyUser" in url or (not url and body):
                key = extract_user_key(body, url)
                if key:
                    user_key = key
                    raw_verify = body
            if "getAccessCodeForAdPoweredStuff" in url:
                ad_code = extract_ad_access_code(body, url) or ad_code

        if not user_key:
            raw_verify = self._js_verify()
            user_key = extract_user_key(raw_verify)

        if not user_key:
            # Parent page also boots embeds; last resort.
            log.info("GET %s (parent, last resort)", IMAGEAPI_URL)
            self.tab.get(IMAGEAPI_URL, timeout=timeout)
            time.sleep(4)
            for pkt in self._drain_listen(10):
                url, body = _packet_url_body(pkt)
                key = extract_user_key(body, url)
                if key:
                    user_key = key
                    raw_verify = body
                ad_code = extract_ad_access_code(body, url) or ad_code
            if not user_key:
                raw_verify = self._js_verify()
                user_key = extract_user_key(raw_verify)

        self._listen_stop()

        if not ad_code:
            ad_code = self._js_ad_access()

        return user_key, ad_code, raw_verify

    def _js_verify(self) -> Any:
        try:
            # Must run in the embed origin.
            if "image-generation.perchance.org" not in (self.tab.url or ""):
                self.tab.get(EMBED_URL, timeout=self.settings.browser_timeout)
                time.sleep(2)
            result = self.tab.run_js(VERIFY_JS, as_expr=False)
            log.info("js verifyUser -> %r", _short(result))
            if isinstance(result, dict):
                return result.get("text") or result
            return result
        except Exception as exc:  # noqa: BLE001
            log.warning("js verifyUser failed: %s", exc)
            try:
                result = self.tab.run_js(
                    "return fetch('/api/verifyUser?thread=0&__cacheBust='+Math.random())"
                    ".then(r => r.text())"
                )
                return result
            except Exception as exc2:  # noqa: BLE001
                log.warning("js verifyUser fallback failed: %s", exc2)
                return None

    def _js_ad_access(self) -> str | None:
        try:
            tab = self.browser.new_tab(PERCHANCE_ORIGIN + "/imageapi")
            time.sleep(2)
            result = tab.run_js(AD_ACCESS_JS)
            try:
                tab.close()
            except Exception:
                pass
            log.info("js adAccess -> %r", _short(result))
            if isinstance(result, dict):
                return extract_ad_access_code(result.get("text"))
            return extract_ad_access_code(result)
        except Exception as exc:  # noqa: BLE001
            log.warning("js adAccess failed: %s", exc)
            return None


def _packet_url_body(pkt: Any) -> tuple[str, Any]:
    url = ""
    body: Any = None
    try:
        url = getattr(pkt, "url", None) or ""
        if not url and getattr(pkt, "request", None) is not None:
            url = getattr(pkt.request, "url", "") or ""
        resp = getattr(pkt, "response", None)
        if resp is not None:
            body = getattr(resp, "body", None)
            if body is None:
                body = getattr(resp, "raw_body", None)
        if body is None:
            body = getattr(pkt, "body", None)
    except Exception:
        pass
    return str(url or ""), body


def _short(val: Any, n: int = 180) -> str:
    s = decode_body(val)
    return s if len(s) <= n else s[:n] + "…"
