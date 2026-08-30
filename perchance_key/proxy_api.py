"""Client for the Hugging Face Proxy Miner space.

API (RepopoxRev/proxy-miner v2.1.0):
  GET /api/health
  GET /api/proxies?protocol=&country_code=&anonymity=&source=&sort=&order=&limit=&offset=
  GET /api/proxies/raw
  GET /api/stats
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from curl_cffi import requests as cffi

from .config import Settings, get_settings
from .models import ProxyEndpoint

log = logging.getLogger("perchance_key.proxy")

# curl_cffi talks to the proxy API *without* a mined proxy — this is the
# control plane. Impersonate so Cloudflare-fronted Spaces don't 403.
_IMPERSONATE = "chrome131"


class ProxyAPIError(RuntimeError):
    pass


class ProxyMiner:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.base = self.settings.proxy_api
        self._session = cffi.Session(impersonate=_IMPERSONATE, timeout=30)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}"
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                r = self._session.get(url, params=params or {})
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").lower()
                if "json" in ctype or (r.text[:1] in "{["):
                    return r.json()
                return r.text
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("proxy api %s attempt %d failed: %s", path, attempt, exc)
        raise ProxyAPIError(f"{path} failed: {last}") from last

    def health(self) -> dict[str, Any]:
        data = self._get("/api/health")
        if not isinstance(data, dict):
            raise ProxyAPIError(f"unexpected health payload: {data!r:.200}")
        return data

    def stats(self) -> dict[str, Any]:
        data = self._get("/api/stats")
        if not isinstance(data, dict):
            raise ProxyAPIError(f"unexpected stats payload: {data!r:.200}")
        return data

    def list_proxies(
        self,
        *,
        protocol: str | None = None,
        country_code: str | None = None,
        anonymity: str | None = None,
        source: str | None = None,
        sort: str = "delay",
        order: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[ProxyEndpoint]]:
        params: dict[str, Any] = {
            "sort": sort,
            "order": order,
            "limit": limit,
            "offset": offset,
        }
        if protocol:
            params["protocol"] = protocol
        if country_code:
            params["country_code"] = country_code
        if anonymity:
            params["anonymity"] = anonymity
        if source:
            params["source"] = source
        data = self._get("/api/proxies", params)
        if not isinstance(data, dict):
            raise ProxyAPIError("proxies endpoint did not return JSON object")
        rows = data.get("proxies") or []
        endpoints = []
        for row in rows:
            try:
                endpoints.append(ProxyEndpoint.from_api(row))
            except Exception as exc:  # noqa: BLE001
                log.debug("skip bad proxy row %r: %s", row, exc)
        total = int(data.get("total_available") or data.get("total") or len(endpoints))
        return total, endpoints

    def iter_proxies(
        self,
        *,
        protocol: str | None = None,
        country_code: str | None = None,
        max_pages: int = 8,
        page_size: int = 50,
        **kw: Any,
    ) -> Iterator[ProxyEndpoint]:
        seen: set[str] = set()
        for page in range(max_pages):
            total, rows = self.list_proxies(
                protocol=protocol,
                country_code=country_code,
                limit=page_size,
                offset=page * page_size,
                **kw,
            )
            if not rows:
                break
            for ep in rows:
                if ep.address in seen or not ep.host or not ep.port:
                    continue
                seen.add(ep.address)
                yield ep
            if (page + 1) * page_size >= total:
                break

    def pick(
        self,
        *,
        protocol: str | None = None,
        country_code: str | None = None,
        exclude: set[str] | None = None,
    ) -> ProxyEndpoint:
        exclude = exclude or set()
        protocols = [protocol] if protocol else list(self.settings.prefer_protocols)
        for proto in protocols:
            for ep in self.iter_proxies(protocol=proto, country_code=country_code, max_pages=4):
                if ep.address in exclude:
                    continue
                yield_ep = ep
                return yield_ep
        raise ProxyAPIError("no unused proxy available from miner API")
