"""
farm_userkeys.py — Multi-proxy userKey harvester.

What it does:
  - For each proxy in your list, do a full perchance flow:
    1. GET the imageapi page (curl_cffi + proxy)
    2. GET the embed page
    3. GET adAccessCode
    4. Try verifyUser (with a NEW thread=0/1/2 each time)
  - Save every successful userKey to .perchance_client/userkeys/farm/<proxy>.json
  - Run N workers in parallel

Why proxies help:
  - Each proxy = a different IP. Cloudflare's "you look suspicious" is
    per-IP. With 50 proxies you have 50 chances.
  - A residential or mobile proxy is more likely to bypass than a datacenter
    one because residential IPs have a history of being "real people".
  - Even if 80% of proxies are blocked, you only need 1 to work.

Usage:
  python farm_userkeys.py --proxies proxies.txt --workers 10 --out-dir .perchance_client/userkeys/farm
  python farm_userkeys.py --proxies-live .perchance_client/live_proxies.json --workers 20
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

try:
    from curl_cffi import requests as cffi_requests
    from curl_cffi.requests.exceptions import RequestException
    _CURL_CFFI_AVAILABLE = True
except Exception:
    cffi_requests = None
    RequestException = Exception
    _CURL_CFFI_AVAILABLE = False

try:
    import certifi
    _CERTIFI_PATH = certifi.where()
except Exception:
    _CERTIFI_PATH = None


# Same endpoints as deep_logger.py
EMBED_URL = "https://image-generation.perchance.org/embed"
VERIFY_USER_URL = "https://image-generation.perchance.org/api/verifyUser"
AD_CODE_URL = "https://perchance.org/api/getAccessCodeForAdPoweredStuff"
IMAGEAPI_URL = "https://perchance.org/imageapi?prompt=a%20cute%20booy"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def load_proxies(path: Path) -> list[str]:
    """Load proxies from a text file (one per line: ip:port or user:pass@ip:port)
    or a JSON file (list of strings or list of dicts with 'proxy' key)."""
    if not path.exists():
        raise FileNotFoundError(f"proxies file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        # JSON
        data = json.loads(text)
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    p = item.get("proxy") or item.get("ip:port") or item.get("url")
                    if p:
                        out.append(p)
            return out
    # Plain text
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def make_proxy_url(p: str) -> str:
    """Ensure proxy is in 'http://ip:port' or 'http://user:pass@ip:port' form."""
    if "://" in p:
        return p
    return f"http://{p}"


def attempt_one_proxy(proxy: str, *, timeout: float = 25.0,
                      fp: str = "chrome150", log_prefix: str = "") -> dict:
    """Try to get a userKey through this proxy. Returns a dict with
    {proxy, status, userKey, adCode, error, requests_made}.
    """
    result: dict = {
        "proxy": proxy,
        "fp": fp,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "unknown",
        "userKey": None,
        "adCode": None,
        "error": None,
        "requests_made": 0,
        "log": [],
    }
    proxy_url = make_proxy_url(proxy)

    def _log(msg: str) -> None:
        result["log"].append(msg)
        if log_prefix:
            print(f"{log_prefix}  {msg}", flush=True)

    sess = cffi_requests.Session(impersonate=fp)
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": '"Chromium";v="150", "Not_A Brand";v="24", "Google Chrome";v="150"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    })
    if _CERTIFI_PATH and os.path.exists(_CERTIFI_PATH):
        try:
            sess.verify = _CERTIFI_PATH
        except Exception:
            pass
    sess.proxies = {"http": proxy_url, "https": proxy_url}

    def do(method: str, url: str, **kw) -> Optional[Any]:
        result["requests_made"] += 1
        try:
            r = sess.request(method, url, timeout=timeout,
                             allow_redirects=True, **kw)
            return r
        except Exception as e:
            _log(f"  ERR {method} {url[:80]}: {type(e).__name__}: {str(e)[:100]}")
            return None

    # Step 1: hit the main page first (gets us any cookies + warm path)
    _log(f"step 1: GET {IMAGEAPI_URL[:60]}")
    r = do("GET", IMAGEAPI_URL)
    if r is None:
        result["status"] = "main_page_failed"
        return result
    if r.status_code != 200:
        result["status"] = f"main_page_{r.status_code}"
        return result
    _log(f"  → {r.status_code} {len(r.content):,}B")

    # Step 2: embed page
    _log("step 2: GET embed")
    r = do("GET", EMBED_URL)
    if r is None:
        result["status"] = "embed_failed"
        return result
    if r.status_code != 200:
        result["status"] = f"embed_{r.status_code}"
        return result
    _log(f"  → {r.status_code} {len(r.content):,}B")

    # Step 3: adAccessCode
    _log("step 3: GET adAccessCode")
    r = do("GET", f"{AD_CODE_URL}?__cacheBust={random.random()}")
    if r is not None and r.status_code == 200:
        body = (r.text or "").strip().strip('"')
        if re.fullmatch(r"[a-f0-9]{64}", body):
            result["adCode"] = body
            _log(f"  → adCode: {body[:16]}…")

    # Step 4: verifyUser — try all 3 threads, take the first that returns userKey
    user_key = None
    for thread_id in range(3):
        _log(f"step 4.{thread_id}: verifyUser thread={thread_id}")
        r = do("GET", f"{VERIFY_USER_URL}?thread={thread_id}"
                     f"&__cacheBust={random.random()}")
        if r is None:
            continue
        if r.status_code != 200:
            _log(f"  → {r.status_code}")
            continue
        body = r.text or ""
        m = re.search(r'"userKey"\s*:\s*"([a-f0-9]{64})"', body)
        if m:
            user_key = m.group(1)
            _log(f"  → userKey: {user_key[:16]}…")
            break
        if "token_required" in body:
            _log(f"  → server wants Turnstile token")
        elif "already_verified" in body:
            _log(f"  → already_verified but no userKey in body?")
        else:
            _log(f"  → body: {body[:80]}")

    if user_key:
        result["userKey"] = user_key
        result["status"] = "success"
    else:
        result["status"] = "verifyUser_no_userkey"

    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="farm_userkeys.py",
        description="Multi-proxy userKey harvester using curl_cffi.",
    )
    ap.add_argument("--proxies", default="proxies.txt",
                    help="Proxies file (txt or json)")
    ap.add_argument("--proxies-live", default=None,
                    help="JSON file from healthchecked proxies")
    ap.add_argument("--out-dir", default=".perchance_client/userkeys/farm",
                    help="Where to save per-proxy results")
    ap.add_argument("--workers", type=int, default=10,
                    help="Parallel workers (default: 10)")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--fp", default="chrome150",
                    help="curl_cffi fingerprint (default: chrome150)")
    ap.add_argument("--max-proxies", type=int, default=0,
                    help="Limit number of proxies (0 = all)")
    ap.add_argument("--retry", type=int, default=1,
                    help="Times to retry each proxy (default: 1)")
    args = ap.parse_args(argv[1:])

    if not _CURL_CFFI_AVAILABLE:
        print("curl_cffi is not installed. Run:")
        print("  pip install --break-system-packages curl_cffi certifi")
        return 1

    # Load proxies
    if args.proxies_live:
        proxy_path = Path(args.proxies_live)
    else:
        proxy_path = Path(args.proxies)
    proxies = load_proxies(proxy_path)
    if args.max_proxies > 0:
        proxies = proxies[: args.max_proxies]
    print(f"loaded {len(proxies)} proxies from {proxy_path}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"running {len(proxies)} proxies × {args.retry} retries "
          f"with {args.workers} workers, fp={args.fp}")
    print("=" * 78)

    successes = []
    failures = []
    lock = threading.Lock()

    def _run(p: str, attempt: int) -> None:
        prefix = f"[{p:<22}] attempt {attempt}"
        r = attempt_one_proxy(p, timeout=args.timeout, fp=args.fp,
                              log_prefix=prefix)
        with lock:
            if r["status"] == "success":
                successes.append(r)
            else:
                failures.append(r)
        # Always save the per-proxy result
        proxy_safe = p.replace(":", "_").replace("@", "_at_").replace("/", "_")
        out_file = out_dir / f"{proxy_safe}_attempt{attempt}.json"
        out_file.write_text(json.dumps(r, indent=2), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = []
        for p in proxies:
            for attempt in range(1, args.retry + 1):
                futures.append(ex.submit(_run, p, attempt))
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"  worker crashed: {e}")

    # Summary
    print()
    print("=" * 78)
    print(f"SUCCESS: {len(successes)}/{len(proxies)} proxies")
    print(f"FAILURE: {len(failures)}/{len(proxies)} proxies")
    if successes:
        print()
        print("Captured userKeys:")
        for r in successes:
            print(f"  {r['proxy']:<25} → {r['userKey']}")
    if failures:
        print()
        print("Failure breakdown:")
        from collections import Counter
        breakdown = Counter(r["status"] for r in failures)
        for status, count in breakdown.most_common():
            print(f"  {count:3d}× {status}")

    # Save summary
    summary_file = out_dir / "summary.json"
    summary_file.write_text(json.dumps({
        "successes": [r["userKey"] for r in successes],
        "proxy_to_userkey": {r["proxy"]: r["userKey"] for r in successes},
        "failures_by_status": dict(Counter(r["status"] for r in failures)),
        "total_proxies": len(proxies),
        "fp": args.fp,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    print(f"\nsummary saved to: {summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
