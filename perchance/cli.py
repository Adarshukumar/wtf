from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from . import __version__

ROOT = Path(__file__).resolve().parent.parent
KEYS = ROOT / "data" / "keys.jsonl"


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _save_key(rec: dict) -> None:
    KEYS.parent.mkdir(parents=True, exist_ok=True)
    with KEYS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _load_latest() -> dict | None:
    if not KEYS.exists():
        return None
    lines = [ln for ln in KEYS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def _resolve_proxy(proxy: str | None, from_api: bool, no_proxy: bool, protocol: str):
    from .proxy import parse_proxy_url, pick

    if no_proxy:
        return None
    if proxy:
        return parse_proxy_url(proxy)
    if from_api:
        return pick(protocol=protocol)
    return None


@click.group()
@click.version_option(__version__)
@click.option("-v", "--verbose", is_flag=True)
def main(verbose: bool) -> None:
    """Perchance userKey (DrissionPage) + generate (curl_cffi)."""
    _log(verbose)


@main.command("proxy-list")
@click.option("--protocol", default="HTTP", show_default=True)
@click.option("--limit", default=15, show_default=True)
def proxy_list(protocol: str, limit: int) -> None:
    from .proxy import health, list_proxies

    click.echo(json.dumps(health(), indent=2))
    rows = list_proxies(protocol=protocol, limit=limit)
    for p in rows:
        click.echo(f"{p.url:32}  {p.country or '--'}")


@main.command("extract")
@click.option("--no-proxy", is_flag=True, help="Direct connection, no proxy.")
@click.option("--proxy", default=None, help="e.g. http://ip:port or socks5://ip:port")
@click.option("--from-api", is_flag=True, help="Pick one proxy from the HF miner API.")
@click.option("--protocol", default="HTTP", show_default=True, help="When using --from-api")
@click.option("--headless", is_flag=True, help="Usually breaks Turnstile. Prefer Xvfb + headed.")
@click.option("--timeout", default=120, show_default=True, type=float)
@click.option("--prompt", default="a cute booy", show_default=True, help="imageapi ?prompt= — same as the HAR")
def extract_cmd(no_proxy, proxy, from_api, protocol, headless, timeout, prompt):
    """Open perchance.org/imageapi, catch userKey from embed iframe network logs."""
    from .extract import extract_user_key

    if not no_proxy and not proxy and not from_api:
        from_api = True
    px = _resolve_proxy(proxy, from_api, no_proxy, protocol)
    rec = extract_user_key(px, headless=headless, timeout=timeout, prompt=prompt)
    _save_key(rec)
    click.echo(json.dumps(rec, indent=2))


@main.command("generate")
@click.option("--prompt", required=True)
@click.option("--key", "user_key", default=None, help="userKey. Default: last extracted.")
@click.option("--no-proxy", is_flag=True)
@click.option("--proxy", default=None, help="Must be the same proxy that minted --key.")
@click.option("--from-api", is_flag=True)
@click.option("--protocol", default="HTTP")
@click.option("--resolution", default="512x768", show_default=True)
@click.option("--channel", default="imageapi", show_default=True)
@click.option("-o", "--out", type=click.Path(path_type=Path), default=None)
def generate_cmd(prompt, user_key, no_proxy, proxy, from_api, protocol, resolution, channel, out):
    """Generate with curl_cffi (no Chrome). Uses the same proxy the key was minted on."""
    from .generate import Client

    rec = _load_latest() or {}
    key = user_key or rec.get("user_key")
    if not key:
        raise click.ClickException("no userKey — run extract first or pass --key")

    if not no_proxy and not proxy and not from_api:
        if rec.get("proxy"):
            proxy = rec["proxy"]
        else:
            no_proxy = True
    px = _resolve_proxy(proxy, from_api, no_proxy, protocol)
    dest = out or (ROOT / "output" / "image.jpg")

    client = Client(key, proxy=px, ad_access_code=rec.get("ad_access_code"))
    try:
        if not client.ad_access_code:
            try:
                client.refresh_ad_access()
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("perchance").warning("adAccessCode skip: %s", exc)
        result = client.generate(prompt, resolution=resolution, channel=channel)
        path = client.download(result, dest)
        click.echo(json.dumps({"result": {k: result.get(k) for k in (
            "status", "imageId", "fileExtension", "seed", "width", "height", "imageDownloadUrl", "maybeNsfw"
        )}, "file": str(path)}, indent=2))
    finally:
        client.close()


@main.command("run")
@click.option("--prompt", required=True)
@click.option("--no-proxy", is_flag=True)
@click.option("--proxy", default=None)
@click.option("--from-api", is_flag=True)
@click.option("--protocol", default="HTTP")
@click.option("--headless", is_flag=True)
@click.option("--resolution", default="512x768")
@click.option("-o", "--out", type=click.Path(path_type=Path), default=None)
def run_cmd(prompt, no_proxy, proxy, from_api, protocol, headless, resolution, out):
    """Extract a userKey then generate that prompt. One proxy for both steps."""
    from .extract import extract_user_key
    from .generate import Client

    if not no_proxy and not proxy and not from_api:
        from_api = True
    px = _resolve_proxy(proxy, from_api, no_proxy, protocol)
    rec = extract_user_key(px, headless=headless, prompt=prompt)
    _save_key(rec)
    click.echo(json.dumps(rec, indent=2), err=True)

    dest = out or (ROOT / "output" / "image.jpg")
    client = Client(rec["user_key"], proxy=px, ad_access_code=rec.get("ad_access_code"))
    try:
        if not client.ad_access_code:
            try:
                client.refresh_ad_access()
            except Exception:
                pass
        result = client.generate(prompt, resolution=resolution)
        path = client.download(result, dest)
        click.echo(json.dumps({"user_key": rec["user_key"], "file": str(path), "imageId": result.get("imageId")}, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
