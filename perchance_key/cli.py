from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from . import __version__
from .config import ROOT, get_settings
from .har import analyze
from .models import ProxyEndpoint
from .store import KeyStore


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.version_option(__version__)
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Perchance userKey extractor — one proxy per Chromium instance."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["settings"] = get_settings()


@main.command("analyze-har")
@click.argument("har_path", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("-o", "--out", type=click.Path(path_type=Path), default=None)
def analyze_har(har_path: Path | None, out: Path | None) -> None:
    """Reconstruct the live protocol from the captured Chrome HAR."""
    path = har_path or (ROOT / "perchance.org.json")
    if not path.exists():
        raise click.ClickException(f"HAR not found: {path}")
    facts = analyze(path)
    payload = facts.as_dict()
    text = json.dumps(payload, indent=2)
    click.echo(text)
    if out:
        out.write_text(text, encoding="utf-8")
        click.echo(f"wrote {out}", err=True)


@main.command("proxy-health")
def proxy_health() -> None:
    """Hit the Hugging Face proxy-miner control plane."""
    from .proxy_api import ProxyMiner

    miner = ProxyMiner()
    try:
        health = miner.health()
        click.echo(json.dumps(health, indent=2))
    finally:
        miner.close()


@main.command("proxy-list")
@click.option("--protocol", default="HTTP", show_default=True)
@click.option("--country", "country_code", default=None)
@click.option("--limit", default=15, show_default=True)
def proxy_list(protocol: str, country_code: str | None, limit: int) -> None:
    from .proxy_api import ProxyMiner

    miner = ProxyMiner()
    try:
        total, rows = miner.list_proxies(protocol=protocol, country_code=country_code, limit=limit)
        click.echo(f"# {len(rows)} / {total}  protocol={protocol} country={country_code or '*'}")
        for ep in rows:
            click.echo(f"{ep.url:32}  {ep.country_code or '--':4}  {ep.anonymity or '-':12}  delay={ep.delay or '-'}")
    finally:
        miner.close()


@main.command("extract")
@click.option("-n", "--count", default=1, show_default=True, help="How many keys (each gets its own proxy).")
@click.option("--protocol", default=None, help="HTTP | HTTPS | SOCKS5 (default: try in that order)")
@click.option("--country", "country_code", default=None)
@click.option("--proxy", "proxy_url", default=None, help="Force a single proxy URL, e.g. http://ip:port")
@click.option("--no-probe", is_flag=True, help="Skip ipify probe (faster, more failures).")
@click.option("--tries", default=12, show_default=True)
def extract_cmd(
    count: int,
    protocol: str | None,
    country_code: str | None,
    proxy_url: str | None,
    no_probe: bool,
    tries: int,
) -> None:
    """Mint userKey(s). Each key is extracted through a dedicated proxy."""
    from .pipeline import Pipeline

    pipe = Pipeline()
    forced = _parse_proxy_url(proxy_url) if proxy_url else None
    try:
        if count == 1:
            bundle = pipe.extract_one(
                protocol=protocol,
                country_code=country_code,
                proxy=forced,
                max_tries=tries,
                probe=not no_probe,
            )
            click.echo(json.dumps(bundle.to_record(), indent=2))
        else:
            if forced:
                raise click.ClickException("--proxy cannot be combined with --count > 1")
            bundles = pipe.extract_many(
                count, protocol=protocol, country_code=country_code, max_tries_each=tries
            )
            click.echo(json.dumps([b.to_record() for b in bundles], indent=2))
    finally:
        pipe.close()


@main.command("keys")
def keys_cmd() -> None:
    """List minted keys (each row is bound to the proxy that created it)."""
    store = KeyStore()
    rows = store.list_keys()
    click.echo(json.dumps(rows, indent=2, default=str))
    click.echo(f"# {len(rows)} key(s)", err=True)


@main.command("generate")
@click.option("--prompt", required=True)
@click.option("--key", "user_key", default=None, help="userKey hex; default = latest stored")
@click.option("--negative", default="")
@click.option("--resolution", default="512x768", show_default=True)
@click.option("--seed", default=-1, type=int)
def generate_cmd(prompt: str, user_key: str | None, negative: str, resolution: str, seed: int) -> None:
    """Call /api/generate through the same proxy that minted the key."""
    store = KeyStore()
    bundle = None
    if user_key:
        for b in store.iter_bundles():
            if b.user_key == user_key:
                bundle = b
                break
        if bundle is None:
            raise click.ClickException(f"unknown userKey {user_key}")
    else:
        bundle = store.latest()
        if bundle is None:
            raise click.ClickException("no stored keys — run extract first")

    sess = BoundSession(bundle.proxy, cookies=bundle.cookies)
    try:
        result = sess.generate(
            bundle,
            prompt,
            negative_prompt=negative,
            seed=seed,
            resolution=resolution,
        )
        click.echo(json.dumps(result, indent=2, default=str))
    finally:
        sess.close()


def _parse_proxy_url(url: str) -> ProxyEndpoint:
    raw = url.strip()
    scheme = "http"
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    host, _, port_s = rest.rpartition(":")
    if scheme.lower() == "https":
        scheme = "http"
    return ProxyEndpoint(host=host, port=int(port_s), protocol=scheme.lower())


if __name__ == "__main__":
    sys.exit(main())
