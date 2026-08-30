"""Mint a userKey with DrissionPage through exactly one proxy."""

from __future__ import annotations

import logging

from .browser import BrowserError, ProxiedChrome
from .config import Settings, get_settings
from .http import BoundSession
from .models import KeyBundle, ProxyEndpoint
from .parse import extract_user_key

log = logging.getLogger("perchance_key.extract")


class ExtractError(RuntimeError):
    pass


def extract_key(
    proxy: ProxyEndpoint,
    settings: Settings | None = None,
    *,
    also_ad_code: bool = True,
) -> KeyBundle:
    """Boot Chromium on `proxy`, load /embed, capture verifyUser.

    The returned bundle is permanently bound to that proxy.
    """
    settings = settings or get_settings()
    if not proxy.host or not proxy.port:
        raise ExtractError(f"invalid proxy: {proxy}")

    with ProxiedChrome(proxy, settings) as chrome:
        user_key, ad_code, raw = chrome.mint_user_key()
        cookies = chrome.cookies_dict()

    if not user_key:
        # Last chance: curl_cffi on the same proxy (works only if CF is
        # lenient for this IP — still IP-correct).
        log.info("browser missed userKey — trying curl_cffi verifyUser on same proxy")
        sess = BoundSession(proxy, settings, cookies=cookies)
        try:
            verified = sess.verify_user()
            user_key = verified.get("user_key") or extract_user_key(verified.get("body"))
            raw = verified.get("body")
            if also_ad_code and not ad_code:
                try:
                    ad_code = sess.get_ad_access_code()
                except Exception as exc:  # noqa: BLE001
                    log.warning("adAccess via curl_cffi failed: %s", exc)
        finally:
            sess.close()

    if not user_key:
        raise ExtractError(
            f"no userKey from {proxy.address} — Cloudflare blocked or proxy dead"
        )

    if also_ad_code and not ad_code:
        sess = BoundSession(proxy, settings, cookies=cookies)
        try:
            ad_code = sess.get_ad_access_code()
        except Exception as exc:  # noqa: BLE001
            log.warning("adAccess follow-up failed: %s", exc)
        finally:
            sess.close()

    bundle = KeyBundle(
        user_key=user_key,
        ad_access_code=ad_code,
        proxy=proxy,
        source="verifyUser",
        user_agent=settings.user_agent,
        cookies=cookies,
        verify_raw=raw,
        extra={"proxy_exit": None},
    )
    log.info("minted userKey %s… via %s", user_key[:12], proxy.address)
    return bundle


def probe_proxy(proxy: ProxyEndpoint, settings: Settings | None = None) -> str:
    """Return the exit IP or raise. Cheap filter before launching Chrome."""
    settings = settings or get_settings()
    sess = BoundSession(proxy, settings)
    try:
        return sess.probe_exit_ip()
    finally:
        sess.close()
