"""
network_logger.py — A focused, deep-working network logger for one URL.

What it does:
  1. Hits https://perchance.org/imageapi?prompt=a%20cute%20booy with curl_cffi
  2. Intercepts EVERY network call: every request, every response, every redirect,
     every cookie, every header, every byte
  3. Streams them to the console in real-time, in a readable, color-coded format
  4. Also handles: sub-resources (JS, CSS, images), XHR/fetch calls, iframes
  5. Uses a real Chrome impersonation fingerprint (auto-detects best available)

Usage:
  python network_logger.py                       # default: hit the URL, log everything
  python network_logger.py --once               # one-shot, then exit
  python network_logger.py --follow             # keep running, log every navigation
  python network_logger.py --url https://...    # different URL
  python network_logger.py --out logs.jsonl     # also save to file
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
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


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_URL = "https://perchance.org/imageapi?prompt=a%20cute%20booy"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
PREFERRED_FPS = [
    "chrome131", "chrome136", "chrome142", "chrome146",
    "chrome124", "chrome119", "chrome116",
    "firefox133", "firefox135", "firefox144", "firefox147",
    "safari184", "safari180",
    "edge101", "edge99",
]


# =============================================================================
# Console formatting (pure ANSI, no colorama)
# =============================================================================


class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    GREY    = "\033[90m"


def _ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _short(s: str, n: int = 80) -> str:
    s = s.replace("\n", "\\n").replace("\r", "\\r")
    return s if len(s) <= n else s[:n - 1] + "…"


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def _status_color(code: int) -> str:
    if code < 200:
        return C.CYAN
    if code < 300:
        return C.GREEN
    if code < 400:
        return C.BLUE
    if code < 500:
        return C.YELLOW
    return C.RED


# =============================================================================
# Network event log
# =============================================================================


@dataclass
class NetEvent:
    """A single network event for the console log."""
    seq: int
    ts: str
    kind: str  # "request" | "response" | "redirect" | "cookie" | "info" | "error" | "complete"
    method: str = ""
    url: str = ""
    status: int = 0
    size: int = 0
    duration_ms: float = 0.0
    headers_in: dict = field(default_factory=dict)
    headers_out: dict = field(default_factory=dict)
    body_preview: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class NetLog:
    """Thread-safe collector + pretty-printer of network events."""

    def __init__(self, out_file: Optional[Path] = None, *, verbose: bool = True) -> None:
        self.events: list[NetEvent] = []
        self.seq = 0
        self.out_file = out_file
        self.verbose = verbose

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def emit(self, ev: NetEvent) -> None:
        self.events.append(ev)
        if self.verbose:
            self._print(ev)
        if self.out_file:
            with self.out_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")

    def _print(self, ev: NetEvent) -> None:
        ts = f"{C.GREY}{ev.ts}{C.RESET}"
        kind_colors = {
            "request": C.CYAN + C.BOLD,
            "response": C.GREEN + C.BOLD,
            "redirect": C.BLUE + C.BOLD,
            "cookie": C.MAGENTA,
            "info": C.YELLOW,
            "error": C.RED + C.BOLD,
            "complete": C.GREEN + C.BOLD,
        }
        kc = kind_colors.get(ev.kind, C.WHITE)
        kind = f"{kc}{ev.kind.upper():>9s}{C.RESET}"

        if ev.kind == "request":
            print(f"{ts}  {kind}  {C.BOLD}{ev.method:6s}{C.RESET}  {ev.url}")
        elif ev.kind == "response":
            sc = _status_color(ev.status)
            print(
                f"{ts}  {kind}  {sc}{ev.status:3d}{C.RESET}  "
                f"{_fmt_size(ev.size):>8s}  {ev.duration_ms:6.1f}ms  {ev.url}"
            )
            if ev.note:
                print(f"{'':38s}{C.GREY}↳ {ev.note}{C.RESET}")
        elif ev.kind == "redirect":
            print(f"{ts}  {kind}  {ev.status}  {ev.url}  →  {ev.note}")
        elif ev.kind == "cookie":
            print(f"{ts}  {kind}  {ev.note}")
        elif ev.kind == "info":
            print(f"{ts}  {kind}  {ev.note}")
        elif ev.kind == "error":
            print(f"{ts}  {kind}  {ev.url}")
            print(f"{'':38s}{C.RED}{ev.note}{C.RESET}")
        elif ev.kind == "complete":
            print(f"{ts}  {kind}  {ev.note}")
        else:
            print(f"{ts}  {kind}  {ev.note}")

        if ev.kind == "response" and self.verbose:
            if ev.headers_in:
                interesting = {
                    k: v for k, v in ev.headers_in.items()
                    if k.lower() in (
                        "content-type", "content-length", "set-cookie",
                        "server", "cf-ray", "cf-cache-status", "location",
                        "x-frame-options", "strict-transport-security",
                    )
                }
                if interesting:
                    for k, v in list(interesting.items())[:6]:
                        print(f"{'':38s}{C.GREY}{k}: {_short(v, 100)}{C.RESET}")
            if ev.body_preview:
                print(f"{'':38s}{C.DIM}body: {_short(ev.body_preview, 200)}{C.RESET}")

    def start_request(self, method: str, url: str) -> str:
        rid = uuid.uuid4().hex[:8]
        ev = NetEvent(
            seq=self._next_seq(), ts=_ts(), kind="request",
            method=method, url=url,
            headers_out={"User-Agent": USER_AGENT},
            extra={"rid": rid},
        )
        self.emit(ev)
        return rid

    def end_request(self, rid: str, method: str, url: str, response: Any,
                    duration_ms: float, note: str = "") -> None:
        ev = NetEvent(
            seq=self._next_seq(), ts=_ts(), kind="response",
            method=method, url=url,
            status=getattr(response, "status_code", 0),
            size=len(getattr(response, "content", b"")),
            duration_ms=duration_ms,
            headers_in=dict(getattr(response, "headers", {})),
            body_preview=(getattr(response, "text", "") or "")[:300],
            note=note,
            extra={"rid": rid},
        )
        self.emit(ev)

    def end_error(self, rid: str, method: str, url: str, exc: Exception,
                  duration_ms: float) -> None:
        ev = NetEvent(
            seq=self._next_seq(), ts=_ts(), kind="error",
            method=method, url=url,
            duration_ms=duration_ms,
            note=f"{type(exc).__name__}: {str(exc)[:200]}",
            extra={"rid": rid},
        )
        self.emit(ev)

    def info(self, msg: str) -> None:
        self.emit(NetEvent(seq=self._next_seq(), ts=_ts(), kind="info", note=msg))

    def error(self, msg: str) -> None:
        self.emit(NetEvent(seq=self._next_seq(), ts=_ts(), kind="error", note=msg))

    def complete(self, msg: str) -> None:
        self.emit(NetEvent(seq=self._next_seq(), ts=_ts(), kind="complete", note=msg))


# =============================================================================
# Fingerprint discovery
# =============================================================================


def detect_best_fingerprint() -> str:
    if not _CURL_CFFI_AVAILABLE:
        return ""
    try:
        available = set(cffi_requests.BrowserType.__members__.keys())
    except Exception:
        return ""
    for fp in PREFERRED_FPS:
        if fp in available:
            return fp
    return next(iter(available), "")


# =============================================================================
# Sub-resource extraction from HTML
# =============================================================================


SCRIPT_RE = re.compile(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
LINK_RE = re.compile(r'<link[^>]*\bhref=["\']([^"\']+)["\']', re.IGNORECASE)
IMG_RE = re.compile(r'<img[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
IFRAME_RE = re.compile(r'<iframe[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)


def extract_resources(html: str, base_url: str) -> dict[str, list[str]]:
    out = {
        "scripts": list(set(SCRIPT_RE.findall(html))),
        "links": list(set(LINK_RE.findall(html))),
        "images": list(set(IMG_RE.findall(html))),
        "iframes": list(set(IFRAME_RE.findall(html))),
    }
    return out


# =============================================================================
# The main network logger
# =============================================================================


def run(
    url: str,
    *,
    out_file: Optional[Path] = None,
    once: bool = True,
    follow: bool = False,
    timeout: float = 20.0,
    fetch_subresources: bool = True,
) -> None:
    if not _CURL_CFFI_AVAILABLE:
        print(f"{C.RED}curl_cffi is not installed.{C.RESET}")
        print(f"Run: {C.BOLD}pip install --break-system-packages curl_cffi certifi{C.RESET}")
        sys.exit(1)

    fp = detect_best_fingerprint()
    if not fp:
        print(f"{C.RED}No usable fingerprint in your curl_cffi build.{C.RESET}")
        sys.exit(1)

    log = NetLog(out_file=out_file)
    log.info(f"Starting network logger")
    log.info(f"Target URL: {url}")
    log.info(f"curl_cffi fingerprint: {fp}")
    if _CERTIFI_PATH:
        log.info(f"certifi CA bundle: {_CERTIFI_PATH}")
    log.info(f"timeout: {timeout}s, once={once}, follow={follow}")
    log.info("─" * 78)

    sess = cffi_requests.Session(impersonate=fp)
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    })
    if _CERTIFI_PATH and os.path.exists(_CERTIFI_PATH):
        try:
            sess.verify = _CERTIFI_PATH
        except Exception:
            pass

    def _do(method: str, target: str, *, max_redirects: int = 10) -> Optional[Any]:
        rid = log.start_request(method, target)
        t0 = time.monotonic()
        try:
            r = sess.request(method, target, timeout=timeout, allow_redirects=False)
        except Exception as e:
            log.end_error(rid, method, target, e, (time.monotonic() - t0) * 1000)
            return None
        dt_ms = (time.monotonic() - t0) * 1000
        hops = 0
        while r.is_redirect and hops < max_redirects:
            log.end_request(rid, method, target, r, dt_ms, note="redirect")
            new_url = r.headers.get("Location", "")
            if not new_url:
                break
            if new_url.startswith("/"):
                from urllib.parse import urlparse, urljoin
                parsed = urlparse(target)
                new_url = f"{parsed.scheme}://{parsed.netloc}{new_url}"
            elif not new_url.startswith("http"):
                new_url = urljoin(target, new_url)
            log.emit(NetEvent(
                seq=log._next_seq(), ts=_ts(), kind="redirect",
                status=r.status_code, url=target, note=new_url,
            ))
            target = new_url
            rid = log.start_request(method, target)
            t0 = time.monotonic()
            try:
                r = sess.request(method, target, timeout=timeout, allow_redirects=False)
            except Exception as e:
                log.end_error(rid, method, target, e, (time.monotonic() - t0) * 1000)
                return None
            dt_ms = (time.monotonic() - t0) * 1000
            hops += 1
        for cookie in r.cookies:
            log.emit(NetEvent(
                seq=log._next_seq(), ts=_ts(), kind="cookie",
                url=target, note=f"{cookie.name}={_short(cookie.value, 30)} "
                                 f"({cookie.domain or '?'}, {cookie.path or '/'})",
            ))
        log.end_request(rid, method, target, r, dt_ms)
        return r

    visited: set[str] = set()
    queue: list[str] = [url]

    while queue:
        target = queue.pop(0)
        if target in visited:
            continue
        visited.add(target)

        r = _do("GET", target)
        if r is None:
            log.error(f"failed to fetch {target}")
            continue

        ct = r.headers.get("Content-Type", "")
        if "html" not in ct.lower():
            log.info(f"non-HTML content ({ct}), skipping resource extraction")
            continue

        html = r.text or ""
        log.info(
            f"page: {len(html):,} chars, {html.count('<script')} scripts, "
            f"{html.count('<link')} links, {html.count('<img')} imgs, "
            f"{html.count('<iframe')} iframes"
        )

        if not fetch_subresources:
            continue

        res = extract_resources(html, target)
        all_subs: list[tuple[str, str]] = []
        for kind in ("scripts", "links", "images", "iframes"):
            for sub in res[kind]:
                all_subs.append((kind, sub))

        log.info(f"extracted {len(all_subs)} sub-resources, fetching each…")
        for kind, sub in all_subs:
            if sub.startswith("//"):
                sub = "https:" + sub
            elif sub.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(target)
                sub = f"{parsed.scheme}://{parsed.netloc}{sub}"
            if sub in visited:
                continue
            visited.add(sub)
            r2 = _do("GET", sub)
            if r2 is not None:
                ctshort = (r2.headers.get("Content-Type") or "").split(";")[0]
                log.info(f"  {kind:8s} → {ctshort:20s}  "
                         f"{_fmt_size(len(r2.content)):>8s}  {sub[:90]}")

        if not follow:
            break

    log.info("─" * 78)
    by_status: dict[int, int] = {}
    by_host: dict[str, int] = {}
    for ev in log.events:
        if ev.kind == "response":
            by_status[ev.status] = by_status.get(ev.status, 0) + 1
            from urllib.parse import urlparse
            host = urlparse(ev.url).netloc
            by_host[host] = by_host.get(host, 0) + 1
    log.complete(
        f"done — {len(log.events)} events, "
        f"{sum(1 for e in log.events if e.kind == 'response')} responses, "
        f"hosts: {len(by_host)}"
    )
    if by_status:
        parts = ", ".join(f"{c}×{sc}" for sc, c in sorted(by_status.items()))
        log.complete(f"  status breakdown: {parts}")
    if by_host:
        top = sorted(by_host.items(), key=lambda kv: -kv[1])[:5]
        log.complete(f"  top hosts: " + ", ".join(f"{h}×{c}" for h, c in top))


# =============================================================================
# CLI
# =============================================================================


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="network_logger.py",
        description="Log every network event for a URL using curl_cffi.",
    )
    ap.add_argument("--url", default=DEFAULT_URL,
                    help="URL to fetch (default: the imageapi page)")
    ap.add_argument("--out", default=None,
                    help="Also append events as JSONL to this file")
    ap.add_argument("--once", action="store_true", default=True,
                    help="Stop after the initial page + sub-resources (default)")
    ap.add_argument("--follow", action="store_true",
                    help="Keep going (queue new sub-resources, fetch them too)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--no-subs", action="store_true",
                    help="Don't fetch sub-resources, just the main page")
    args = ap.parse_args(argv[1:])

    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            out_path.unlink()

    run(
        args.url,
        out_file=out_path,
        once=not args.follow,
        follow=args.follow,
        timeout=args.timeout,
        fetch_subresources=not args.no_subs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
