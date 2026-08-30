"""One proxy → one Chromium → one userKey. Never reuse a proxy mid-flight."""

from __future__ import annotations

import logging
from typing import Iterator

from .config import Settings, get_settings
from .extractor import ExtractError, extract_key, probe_proxy
from .models import KeyBundle, ProxyEndpoint
from .proxy_api import ProxyAPIError, ProxyMiner
from .store import KeyStore

log = logging.getLogger("perchance_key.pipeline")


class Pipeline:
    def __init__(self, settings: Settings | None = None, store: KeyStore | None = None):
        self.settings = settings or get_settings()
        self.store = store or KeyStore(settings=self.settings)
        self.miner = ProxyMiner(self.settings)

    def close(self) -> None:
        self.miner.close()

    def _exclude(self) -> set[str]:
        return self.store.dead() | self.store.used_proxies()

    def candidate_proxies(
        self,
        *,
        protocol: str | None = None,
        country_code: str | None = None,
        limit: int = 40,
    ) -> Iterator[ProxyEndpoint]:
        exclude = self._exclude()
        protocols = [protocol] if protocol else list(self.settings.prefer_protocols)
        yielded = 0
        for proto in protocols:
            for ep in self.miner.iter_proxies(protocol=proto, country_code=country_code):
                if ep.address in exclude:
                    continue
                yield ep
                yielded += 1
                if yielded >= limit:
                    return

    def extract_one(
        self,
        *,
        protocol: str | None = None,
        country_code: str | None = None,
        proxy: ProxyEndpoint | None = None,
        max_tries: int = 12,
        probe: bool = True,
    ) -> KeyBundle:
        tried = 0
        last_err: Exception | None = None
        stream: Iterator[ProxyEndpoint]
        if proxy is not None:
            stream = iter([proxy])
        else:
            stream = self.candidate_proxies(protocol=protocol, country_code=country_code, limit=max_tries)

        for ep in stream:
            tried += 1
            log.info("try %d/%d proxy %s (%s %s)", tried, max_tries, ep.address, ep.protocol, ep.country_code or "?")
            try:
                if probe:
                    exit_ip = probe_proxy(ep, self.settings)
                    log.info("proxy %s exit ip %s", ep.address, exit_ip)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning("probe failed %s: %s", ep.address, exc)
                self.store.mark_dead(ep.address, f"probe: {exc}")
                continue
            try:
                bundle = extract_key(ep, self.settings)
                self.store.save(bundle)
                return bundle
            except (ExtractError, Exception) as exc:  # noqa: BLE001
                last_err = exc
                log.warning("extract failed %s: %s", ep.address, exc)
                self.store.mark_dead(ep.address, f"extract: {exc}")
                continue

        raise ProxyAPIError(
            f"could not mint a userKey after {tried} proxied attempts; last error: {last_err}"
        )

    def extract_many(
        self,
        count: int,
        *,
        protocol: str | None = None,
        country_code: str | None = None,
        max_tries_each: int = 12,
    ) -> list[KeyBundle]:
        out: list[KeyBundle] = []
        for i in range(count):
            log.info("=== key %d / %d ===", i + 1, count)
            out.append(
                self.extract_one(
                    protocol=protocol,
                    country_code=country_code,
                    max_tries=max_tries_each,
                )
            )
        return out
