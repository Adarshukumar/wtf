"""Fetch one proxy from the Hugging Face miner API. That is all.

Control plane (not our engine): https://adarshu07-no-plz.hf.space
  GET /api/proxies?protocol=HTTP&sort=delay&order=asc&limit=50
  GET /api/health
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests as cffi

from .urls import DEFAULT_PROXY_API, UA


@dataclass
class Proxy:
    host: str
    port: int
    protocol: str  # http | socks5 | socks4
    country: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        scheme = "socks5" if self.protocol.startswith("socks5") else (
            "socks4" if self.protocol == "socks4" else "http"
        )
        return f"{scheme}://{self.host}:{self.port}"

    def curl_proxies(self) -> dict[str, str]:
        return {"http": self.url, "https": self.url}


def _session():
    s = cffi.Session(impersonate="chrome131", timeout=25)
    s.headers["User-Agent"] = UA
    return s


def api_base() -> str:
    return os.environ.get("PERCHANCE_PROXY_API", DEFAULT_PROXY_API).rstrip("/")


def health() -> dict[str, Any]:
    r = _session().get(f"{api_base()}/api/health")
    r.raise_for_status()
    return r.json()


def list_proxies(
    protocol: str = "HTTP",
    limit: int = 30,
    country_code: str | None = None,
) -> list[Proxy]:
    params: dict[str, Any] = {
        "protocol": protocol,
        "sort": "delay",
        "order": "asc",
        "limit": limit,
    }
    if country_code:
        params["country_code"] = country_code
    r = _session().get(f"{api_base()}/api/proxies", params=params)
    r.raise_for_status()
    data = r.json()
    out: list[Proxy] = []
    for row in data.get("proxies") or []:
        try:
            out.append(_from_row(row, prefer=protocol))
        except Exception:
            continue
    return out


def pick(protocol: str = "HTTP", country_code: str | None = None) -> Proxy:
    rows = list_proxies(protocol=protocol, country_code=country_code, limit=40)
    if not rows:
        raise RuntimeError(f"proxy API returned no {protocol} proxies")
    return rows[0]


def parse_proxy_url(url: str) -> Proxy:
    raw = url.strip()
    scheme, _, rest = raw.partition("://")
    if not rest:
        rest, scheme = raw, "http"
    host, _, port_s = rest.rpartition(":")
    proto = "socks5" if scheme.lower().startswith("socks5") else (
        "socks4" if scheme.lower() == "socks4" else "http"
    )
    return Proxy(host=host, port=int(port_s), protocol=proto)


def _from_row(row: dict[str, Any], prefer: str) -> Proxy:
    addr = str(row.get("proxy") or "")
    host, _, port_s = addr.rpartition(":")
    protocols = row.get("protocols") or []
    if isinstance(protocols, str):
        protocols = [protocols]
    upper = [p.upper() for p in protocols]
    if prefer.upper() in upper:
        proto = prefer.lower()
    elif "HTTP" in upper or "HTTPS" in upper:
        proto = "http"
    elif "SOCKS5" in upper:
        proto = "socks5"
    elif "SOCKS4" in upper:
        proto = "socks4"
    else:
        proto = "http"
    if proto == "https":
        proto = "http"
    return Proxy(
        host=host or str(row.get("ip") or ""),
        port=int(port_s or row.get("port") or 0),
        protocol=proto,
        country=row.get("country_code"),
        raw=row,
    )
