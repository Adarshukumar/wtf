from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProxyEndpoint:
    """One proxy, one instance. Never share across Chromium/curl sessions."""

    host: str
    port: int
    protocol: str  # http | socks5 | socks4
    country_code: str | None = None
    anonymity: str | None = None
    source: str | None = None
    delay: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        scheme = self.protocol.lower()
        if scheme == "https":
            scheme = "http"
        if scheme not in ("http", "socks5", "socks4", "socks5h"):
            scheme = "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def chromium_proxy(self) -> str:
        """Chromium --proxy-server value."""
        scheme = self.protocol.lower()
        if scheme in ("socks5", "socks5h"):
            return f"socks5://{self.host}:{self.port}"
        if scheme == "socks4":
            return f"socks4://{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"

    def curl_proxies(self) -> dict[str, str]:
        return {"http": self.url, "https": self.url}

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "ProxyEndpoint":
        proxy = str(row.get("proxy") or "")
        if ":" not in proxy:
            ip = str(row.get("ip") or "")
            port = int(row.get("port") or 0)
            proxy = f"{ip}:{port}"
        host, _, port_s = proxy.rpartition(":")
        protocols = row.get("protocols") or []
        if isinstance(protocols, str):
            protocols = [p.strip() for p in protocols.replace(",", " ").split() if p.strip()]
        proto = pick_protocol(protocols)
        return cls(
            host=host or str(row.get("ip") or ""),
            port=int(port_s or row.get("port") or 0),
            protocol=proto,
            country_code=row.get("country_code"),
            anonymity=row.get("anonymity"),
            source=row.get("source"),
            delay=str(row.get("delay") or "") or None,
            raw=row,
        )


def pick_protocol(protocols: list[str]) -> str:
    upper = [p.upper() for p in protocols if p]
    for cand in ("HTTP", "HTTPS", "SOCKS5", "SOCKS4"):
        if cand in upper:
            return "socks5" if cand == "SOCKS5" else ("socks4" if cand == "SOCKS4" else "http")
    return "http"


@dataclass
class KeyBundle:
    """A userKey minted through a single proxy, plus the adAccessCode."""

    user_key: str
    ad_access_code: str | None
    proxy: ProxyEndpoint
    source: str  # verifyUser | listen | generate | har
    user_agent: str
    cookies: dict[str, str] = field(default_factory=dict)
    verify_raw: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        rec = {
            "user_key": self.user_key,
            "ad_access_code": self.ad_access_code,
            "proxy": self.proxy.address,
            "proxy_url": self.proxy.url,
            "proxy_protocol": self.proxy.protocol,
            "country_code": self.proxy.country_code,
            "source": self.source,
            "user_agent": self.user_agent,
        }
        rec.update({k: v for k, v in (self.extra or {}).items() if _is_scalar(v)})
        return rec

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["proxy"] = {
            "host": self.proxy.host,
            "port": self.proxy.port,
            "protocol": self.proxy.protocol,
            "url": self.proxy.url,
            "address": self.proxy.address,
            "country_code": self.proxy.country_code,
            "source": self.proxy.source,
        }
        return d


def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None
