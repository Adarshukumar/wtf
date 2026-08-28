"""
deep_logger.py — Drive https://perchance.org/imageapi?prompt=a%20cute%20booy
with curl_cffi, replaying everything the page would do (real HTML/JS discovery
+ iframe + API calls), and stream every network event to:

  1. The console (color-coded, real-time)
  2. A live-updating index.html (open it in your browser while it runs)

This is NOT a real browser — it's curl_cffi pretending to be Chrome
(same TLS fingerprint + same headers) but it crawls the page the way
a real browser would: parses the HTML, finds every script/css/image/iframe,
follows them, and for the embed iframe it replays all the API calls
(verifyUser, generate, await, download) that the real page would make.

Usage:
    python deep_logger.py                       # full deep replay
    python deep_logger.py --out index.html      # also write to HTML
    python deep_logger.py --prompt "a cute boy" # different prompt
    python deep_logger.py --no-iframe           # skip the embed iframe logic
    python deep_logger.py --just-page          # only the main page
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
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

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
]

# Endpoints we know about, from the HAR
WRAPPER_PAGE_URL = "https://perchance.org/imageapi?prompt=a%20cute%20booy"
AD_CODE_URL = "https://perchance.org/api/getAccessCodeForAdPoweredStuff"
EMBED_URL = "https://image-generation.perchance.org/embed"
VERIFY_USER_URL = "https://image-generation.perchance.org/api/verifyUser"
GENERATE_URL = "https://image-generation.perchance.org/api/generate"
AWAIT_URL = "https://image-generation.perchance.org/api/awaitExistingGenerationRequest"
QUEUE_POS_URL = "https://image-generation.perchance.org/api/getUserQueuePosition"
DOWNLOAD_VIA_PROXY_URL = "https://image-generation.perchance.org/api/downloadTemporaryImageViaProxy"

# These are API endpoints the page fetches via JS (not in the HTML directly)
KNOWN_API_ENDPOINTS = [
    "https://perchance.org/api/getCommunityData",
    "https://perchance.org/api/cv",
    "https://perchance.org/api/alc",
    "https://perchance.org/api/count",
    "https://perchance.org/api/securityData",
    "https://perchance.org/api/getAccessCodeForAdPoweredStuff",
]


# =============================================================================
# ANSI colors
# =============================================================================


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GREY = "\033[90m"


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
# Event log
# =============================================================================


@dataclass
class NetEvent:
    seq: int = 0
    ts: str = ""
    kind: str = ""     # request | response | redirect | cookie | info | error | complete
    method: str = ""
    url: str = ""
    status: int = 0
    size: int = 0
    duration_ms: float = 0.0
    request_headers: dict = field(default_factory=dict)
    response_headers: dict = field(default_factory=dict)
    body_preview: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """Thread-safe collector + dual sink (console + HTML)."""

    def __init__(self, html_path: Optional[Path] = None,
                 console: bool = True) -> None:
        self.events: list[NetEvent] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.html_path = html_path
        self.console = console
        self._html_initialized = False
        self.started_at = time.time()
        self._meta = {
            "url": "",
            "fingerprint": "",
            "user_agent": USER_AGENT,
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        if html_path:
            html_path.parent.mkdir(parents=True, exist_ok=True)
            # Don't pre-init; we'll write atomically at the end

    def _next(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def emit(self, ev: NetEvent) -> None:
        if not ev.seq:
            ev.seq = self._next()
        if not ev.ts:
            ev.ts = _ts()
        with self._lock:
            self.events.append(ev)
        if self.console:
            self._print_console(ev)

    def _print_console(self, ev: NetEvent) -> None:
        ts = f"{C.GREY}{ev.ts}{C.RESET}"
        kinds = {
            "request": C.CYAN + C.BOLD,
            "response": C.GREEN + C.BOLD,
            "redirect": C.BLUE + C.BOLD,
            "cookie": C.MAGENTA,
            "info": C.YELLOW,
            "error": C.RED + C.BOLD,
            "complete": C.GREEN + C.BOLD,
        }
        kc = kinds.get(ev.kind, C.WHITE)
        k = f"{kc}{ev.kind.upper():>9s}{C.RESET}"

        if ev.kind == "request":
            print(f"{ts}  {k}  {C.BOLD}{ev.method:6s}{C.RESET}  {ev.url}")
        elif ev.kind == "response":
            sc = _status_color(ev.status)
            print(
                f"{ts}  {k}  {sc}{ev.status:3d}{C.RESET}  "
                f"{_fmt_size(ev.size):>8s}  {ev.duration_ms:6.1f}ms  {ev.url}"
            )
            if ev.note:
                print(f"{'':38s}{C.GREY}↳ {ev.note}{C.RESET}")
            if ev.response_headers:
                interesting = {k: v for k, v in ev.response_headers.items()
                               if k.lower() in (
                                   "content-type", "content-length", "set-cookie",
                                   "server", "cf-ray", "cf-cache-status", "location",
                                   "x-frame-options", "strict-transport-security",
                               )}
                for kk, vv in list(interesting.items())[:5]:
                    print(f"{'':38s}{C.GREY}{kk}: {_short(vv, 100)}{C.RESET}")
            if ev.body_preview:
                print(f"{'':38s}{C.DIM}body: {_short(ev.body_preview, 200)}{C.RESET}")
        elif ev.kind == "redirect":
            print(f"{ts}  {k}  {ev.status}  {ev.url}  →  {ev.note}")
        elif ev.kind == "cookie":
            print(f"{ts}  {k}  {ev.note}")
        elif ev.kind == "info":
            print(f"{ts}  {k}  {ev.note}")
        elif ev.kind == "error":
            print(f"{ts}  {k}  {ev.url}")
            print(f"{'':38s}{C.RED}{ev.note}{C.RESET}")
        elif ev.kind == "complete":
            print(f"{ts}  {k}  {ev.note}")
        else:
            print(f"{ts}  {k}  {ev.note}")

    # ---------- shorthand ----------

    def info(self, msg: str, **extra) -> None:
        ev = NetEvent(kind="info", note=msg, extra=extra)
        self.emit(ev)

    def error(self, msg: str, url: str = "", **extra) -> None:
        ev = NetEvent(kind="error", note=msg, url=url, extra=extra)
        self.emit(ev)

    def complete(self, msg: str) -> None:
        ev = NetEvent(kind="complete", note=msg)
        self.emit(ev)

    def request(self, method: str, url: str, **extra) -> int:
        ev = NetEvent(kind="request", method=method, url=url, extra=extra)
        self.emit(ev)
        return ev.seq

    def response(self, method: str, url: str, status: int, size: int,
                 duration_ms: float, headers: dict, body: str,
                 note: str = "", **extra) -> None:
        ev = NetEvent(
            kind="response", method=method, url=url, status=status,
            size=size, duration_ms=duration_ms,
            response_headers=headers, body_preview=(body or "")[:500],
            note=note, extra=extra,
        )
        self.emit(ev)

    def net_error(self, method: str, url: str, exc: Exception,
                  duration_ms: float) -> None:
        ev = NetEvent(
            kind="error", method=method, url=url, duration_ms=duration_ms,
            note=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        self.emit(ev)

    def redirect(self, status: int, from_url: str, to_url: str) -> None:
        ev = NetEvent(
            kind="redirect", status=status, url=from_url, note=to_url,
        )
        self.emit(ev)

    def cookie(self, name: str, value: str, domain: str, path: str) -> None:
        ev = NetEvent(
            kind="cookie",
            note=f"{name}={_short(value, 30)} ({domain or '?'}, {path or '/'})",
        )
        self.emit(ev)

    # ---------- HTML export ----------

    def write_html(self) -> None:
        if not self.html_path:
            return
        html = self._render_html()
        self.html_path.write_text(html, encoding="utf-8")

    def _render_html(self) -> str:
        with self._lock:
            events = list(self.events)
        # Build rows
        rows = []
        for ev in events:
            row_class = f"row-{ev.kind}"
            if ev.kind in ("response", "request"):
                row_class += f" status-{ev.status // 100}xx" if ev.status else ""
            cells = [
                f'<td class="seq">{ev.seq}</td>',
                f'<td class="ts">{ev.ts}</td>',
                f'<td class="kind kind-{ev.kind}">{ev.kind.upper()}</td>',
            ]
            if ev.kind == "request":
                cells += [
                    f'<td class="method">{ev.method}</td>',
                    f'<td class="url" title="{_short(ev.url, 500)}">'
                    f'<a href="{ev.url}" target="_blank" rel="noopener">'
                    f'{_short(ev.url, 200)}</a></td>',
                    '<td class="status"></td><td class="size"></td>'
                    '<td class="duration"></td><td class="note"></td>',
                ]
            elif ev.kind == "response":
                sc_class = f"status-{ev.status // 100}xx"
                cells += [
                    f'<td class="method">{ev.method}</td>',
                    f'<td class="url" title="{_short(ev.url, 500)}">'
                    f'<a href="{ev.url}" target="_blank" rel="noopener">'
                    f'{_short(ev.url, 200)}</a></td>',
                    f'<td class="status {sc_class}">{ev.status}</td>',
                    f'<td class="size">{_fmt_size(ev.size)}</td>',
                    f'<td class="duration">{ev.duration_ms:.1f}ms</td>',
                    f'<td class="note">{_short(ev.note or "", 100)}</td>',
                ]
                # Detail row with headers + body preview
                detail_parts = []
                if ev.response_headers:
                    headers_html = "<br>".join(
                        f"<b>{_short(k, 30)}</b>: {_short(v, 200)}"
                        for k, v in list(ev.response_headers.items())[:20]
                    )
                    detail_parts.append(
                        f'<div class="headers"><b>Response headers:</b><br>'
                        f'{headers_html}</div>'
                    )
                if ev.body_preview:
                    detail_parts.append(
                        f'<div class="body"><b>Body preview:</b><br>'
                        f'<pre>{_short(ev.body_preview, 1500)}</pre></div>'
                    )
                if detail_parts:
                    cells.append(
                        f'</tr><tr class="detail-row"><td colspan="8">'
                        f'{"".join(detail_parts)}</td>'
                    )
            elif ev.kind == "error":
                cells += [
                    f'<td class="method">{ev.method}</td>',
                    f'<td class="url" title="{_short(ev.url, 500)}">'
                    f'{_short(ev.url, 200)}</td>',
                    '<td class="status"></td><td class="size"></td>'
                    f'<td class="duration">{ev.duration_ms:.1f}ms</td>',
                    f'<td class="note error-note">{_short(ev.note, 200)}</td>',
                ]
            elif ev.kind == "redirect":
                cells += [
                    '<td></td>',
                    f'<td class="url">from: {_short(ev.url, 200)}<br>'
                    f'to:   {_short(ev.note, 200)}</td>',
                    f'<td class="status">{ev.status}</td>',
                    '<td></td><td></td>',
                    '<td class="note">redirect</td>',
                ]
            elif ev.kind == "cookie":
                cells += [
                    '<td></td><td></td><td></td><td></td><td></td>',
                    f'<td colspan="2" class="note">{_short(ev.note, 200)}</td>',
                ]
            else:
                cells += [
                    '<td></td><td></td><td></td><td></td><td></td>',
                    f'<td colspan="2" class="note">{_short(ev.note, 200)}</td>',
                ]
            rows.append(f'<tr class="{row_class.strip()}">{"".join(cells)}</tr>')

        # Stats
        with self._lock:
            n_total = len(self.events)
        n_responses = sum(1 for e in self.events if e.kind == "response")
        n_errors = sum(1 for e in self.events if e.kind == "error")
        n_requests = sum(1 for e in self.events if e.kind == "request")
        from collections import Counter
        status_counts = Counter(e.status for e in self.events
                                if e.kind == "response")
        host_counts = Counter(urlparse(e.url).netloc for e in self.events
                              if e.kind == "response" and e.url)
        top_hosts = host_counts.most_common(5)
        status_pills = " ".join(
            f'<span class="status-pill status-{s // 100}xx">{s}×{c}</span>'
            for s, c in sorted(status_counts.items())
        )
        host_pills = " ".join(
            f'<span class="host-pill">{h}×{c}</span>'
            for h, c in top_hosts
        )

        meta = self._meta
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>deep_logger.py — Network Log</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    background: #0b0e14; color: #d8dee9; margin: 0; padding: 0;
    font-size: 13px;
  }}
  header {{
    background: linear-gradient(180deg, #1a1f2b 0%, #0b0e14 100%);
    border-bottom: 1px solid #2a3140;
    padding: 16px 24px;
    position: sticky; top: 0; z-index: 10;
  }}
  h1 {{ margin: 0 0 8px 0; font-size: 18px; color: #88c0d0; }}
  .meta {{ color: #6a7280; font-size: 12px; line-height: 1.6; }}
  .meta b {{ color: #81a1c1; }}
  .stats {{ margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; }}
  .stat {{ background: #1a1f2b; padding: 4px 10px; border-radius: 4px;
          border: 1px solid #2a3140; font-size: 12px; }}
  .stat b {{ color: #a3be8c; }}
  .status-pill, .host-pill {{
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-size: 11px; margin-right: 4px; font-family: monospace;
  }}
  .status-2xx {{ background: #2d4a2d; color: #a3be8c; }}
  .status-3xx {{ background: #2d3a4a; color: #88c0d0; }}
  .status-4xx {{ background: #4a3a2d; color: #ebcb8b; }}
  .status-5xx {{ background: #4a2d2d; color: #bf616a; }}
  .host-pill {{ background: #1a1f2b; color: #81a1c1; }}
  .controls {{
    padding: 8px 24px; background: #0b0e14; border-bottom: 1px solid #2a3140;
    position: sticky; top: 140px; z-index: 9;
    display: flex; gap: 8px; align-items: center;
  }}
  .controls input {{
    background: #1a1f2b; color: #d8dee9; border: 1px solid #2a3140;
    padding: 4px 8px; border-radius: 3px; font-size: 12px;
    flex: 1; font-family: monospace;
  }}
  .controls button {{
    background: #2d4a2d; color: #a3be8c; border: 1px solid #3a5a3a;
    padding: 4px 12px; border-radius: 3px; cursor: pointer; font-size: 12px;
  }}
  .controls button:hover {{ background: #3a5a3a; }}
  .controls label {{ color: #81a1c1; font-size: 12px; }}
  table {{
    width: 100%; border-collapse: collapse; font-family: monospace;
    font-size: 12px;
  }}
  thead th {{
    background: #1a1f2b; color: #81a1c1; text-align: left;
    padding: 6px 8px; border-bottom: 1px solid #2a3140;
    position: sticky; top: 184px; z-index: 8;
  }}
  tbody tr {{ border-bottom: 1px solid #1a1f2b; }}
  tbody tr:hover {{ background: #1a1f2b; }}
  tbody tr.detail-row {{ background: #11151c; }}
  tbody tr.detail-row td {{ padding: 8px 12px; }}
  td {{ padding: 4px 8px; vertical-align: top; }}
  td.seq {{ color: #6a7280; width: 50px; }}
  td.ts {{ color: #6a7280; width: 100px; white-space: nowrap; }}
  td.kind {{ width: 80px; text-align: center; font-weight: bold; }}
  .kind-request {{ color: #88c0d0; }}
  .kind-response {{ color: #a3be8c; }}
  .kind-redirect {{ color: #81a1c1; }}
  .kind-cookie {{ color: #b48ead; }}
  .kind-info {{ color: #ebcb8b; }}
  .kind-error {{ color: #bf616a; }}
  .kind-complete {{ color: #a3be8c; }}
  td.method {{ color: #d8dee9; width: 60px; }}
  td.url {{ color: #88c0d0; word-break: break-all; }}
  td.url a {{ color: #88c0d0; text-decoration: none; }}
  td.url a:hover {{ text-decoration: underline; }}
  td.status {{ width: 60px; text-align: right; }}
  td.size {{ width: 80px; text-align: right; color: #6a7280; }}
  td.duration {{ width: 80px; text-align: right; color: #6a7280; }}
  td.note {{ color: #6a7280; font-style: italic; }}
  .error-note {{ color: #bf616a; font-style: normal; }}
  .row-error td {{ background: #1a0f10; }}
  .headers, .body {{ margin: 4px 0; font-size: 11px; }}
  .headers b, .body b {{ color: #81a1c1; }}
  pre {{
    background: #11151c; color: #d8dee9; padding: 6px 8px; border-radius: 3px;
    overflow-x: auto; margin: 4px 0; max-width: 100%;
    white-space: pre-wrap; word-break: break-all;
  }}
  .empty {{ padding: 40px; text-align: center; color: #6a7280; }}
  footer {{
    padding: 16px 24px; text-align: center; color: #4a5568; font-size: 11px;
    border-top: 1px solid #1a1f2b;
  }}
</style>
</head>
<body>
<header>
  <h1>🔍 deep_logger.py — Network Log</h1>
  <div class="meta">
    <b>URL:</b> {_short(meta.get("url", ""), 120)}<br>
    <b>Fingerprint:</b> {meta.get("fingerprint", "")} ·
    <b>User-Agent:</b> {_short(meta.get("user_agent", ""), 80)}<br>
    <b>Started:</b> {meta.get("started_at", "")} ·
    <b>Total events:</b> {n_total}
  </div>
  <div class="stats">
    <div class="stat"><b>{n_requests}</b> requests</div>
    <div class="stat"><b>{n_responses}</b> responses</div>
    <div class="stat"><b>{n_errors}</b> errors</div>
    <div class="stat">{status_pills}</div>
    <div class="stat">{host_pills}</div>
  </div>
</header>
<div class="controls">
  <input type="text" id="filter" placeholder="filter by url, status, method…">
  <label><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
  <button onclick="document.getElementById('filter').value=''; applyFilter();">clear</button>
  <button onclick="window.scrollTo(0, document.body.scrollHeight);">↓ bottom</button>
</div>
<table>
  <thead>
    <tr>
      <th>#</th><th>ts</th><th>kind</th>
      <th>method</th><th>url</th>
      <th>status</th><th>size</th><th>duration</th><th>note</th>
    </tr>
  </thead>
  <tbody id="events">
    {''.join(rows) if rows else '<tr><td colspan="9" class="empty">no events yet…</td></tr>'}
  </tbody>
</table>
<footer>
  Generated by <code>deep_logger.py</code> · {n_total} events ·
  auto-refresh every 1s · <span id="last-update"></span>
</footer>
<script>
  function applyFilter() {{
    const q = document.getElementById('filter').value.toLowerCase();
    const rows = document.querySelectorAll('tbody tr');
    for (const r of rows) {{
      r.style.display = !q || r.innerText.toLowerCase().includes(q) ? '' : 'none';
    }}
  }}
  document.getElementById('filter').addEventListener('input', applyFilter);
  // auto-refresh the whole page every 1s while the script is running
  setTimeout(() => location.reload(), 1000);
  document.getElementById('last-update').innerText = 'last refresh: ' + new Date().toLocaleTimeString();
</script>
</body>
</html>"""


# =============================================================================
# HTML resource extraction
# =============================================================================


SCRIPT_RE = re.compile(
    r'<script[^>]*\b(?:src|data-src)=["\']([^"\']+)["\']', re.IGNORECASE
)
LINK_RE = re.compile(
    r'<link[^>]*\bhref=["\']([^"\']+)["\']', re.IGNORECASE
)
IMG_RE = re.compile(
    r'<img[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE
)
IFRAME_RE = re.compile(
    r'<iframe[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE
)
PREFETCH_RE = re.compile(
    r'<link[^>]*\brel=["\']prefetch["\'][^>]*\bhref=["\']([^"\']+)["\']', re.IGNORECASE
)
PRECONNECT_RE = re.compile(
    r'<link[^>]*\brel=["\']preconnect["\'][^>]*\bhref=["\']([^"\']+)["\']', re.IGNORECASE
)


def extract_resources(html: str) -> dict[str, list[str]]:
    return {
        "scripts": list(set(SCRIPT_RE.findall(html))),
        "links": list(set(LINK_RE.findall(html))),
        "images": list(set(IMG_RE.findall(html))),
        "iframes": list(set(IFRAME_RE.findall(html))),
        "prefetch": list(set(PREFETCH_RE.findall(html))),
        "preconnect": list(set(PRECONNECT_RE.findall(html))),
    }


# =============================================================================
# The deep driver
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


class DeepDriver:
    """
    Drives a single user-session through the imageapi page.
    Replays every request the real page would make:
      - Main page (with redirect following)
      - Sub-resources (scripts, css, images, iframes, prefetched)
      - Known API endpoints the page polls
      - The embed iframe: verifyUser + generate + await + download
    """

    def __init__(self, log: EventLog, *, timeout: float = 20.0) -> None:
        self.log = log
        self.timeout = timeout
        self.fp = detect_best_fingerprint()
        self.sess: Optional[Any] = None
        self.visited: set[str] = set()
        self.cookies: dict[str, str] = {}

    def _build_session(self) -> Any:
        sess = cffi_requests.Session(impersonate=self.fp)
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
        return sess

    def _do(self, method: str, url: str, **kw) -> Optional[Any]:
        """Issue a request, follow redirects, log everything."""
        if url in self.visited and method == "GET" and not kw.get("force"):
            self.log.info(f"  (skip already-visited: {url[:100]})")
            return None

        self.log.request(method, url)
        t0 = time.monotonic()
        try:
            r = self.sess.request(method, url, timeout=self.timeout,
                                  allow_redirects=False, **kw)
        except Exception as e:
            dt_ms = (time.monotonic() - t0) * 1000
            self.log.net_error(method, url, e, dt_ms)
            return None
        dt_ms = (time.monotonic() - t0) * 1000

        # Only mark as visited AFTER success (so retries work)
        self.visited.add(url)

        # Follow redirects manually
        hops = 0
        while r.is_redirect and hops < 10:
            self.log.response(method, url, r.status_code,
                              len(r.content), dt_ms,
                              dict(r.headers), r.text, note="redirect")
            new_url = r.headers.get("Location", "")
            if not new_url:
                break
            if new_url.startswith("/"):
                p = urlparse(url)
                new_url = f"{p.scheme}://{p.netloc}{new_url}"
            elif not new_url.startswith("http"):
                new_url = urljoin(url, new_url)
            self.log.redirect(r.status_code, url, new_url)
            url = new_url
            self.log.request(method, url)
            t0 = time.monotonic()
            try:
                r = self.sess.request(method, url, timeout=self.timeout,
                                      allow_redirects=False, **kw)
            except Exception as e:
                dt_ms = (time.monotonic() - t0) * 1000
                self.log.net_error(method, url, e, dt_ms)
                return None
            dt_ms = (time.monotonic() - t0) * 1000
            self.visited.add(url)
            hops += 1

        # Cookies
        for c in r.cookies:
            self.log.cookie(c.name, c.value, c.domain, c.path)
            self.cookies[c.name] = c.value

        self.log.response(method, url, r.status_code, len(r.content), dt_ms,
                          dict(r.headers), r.text)
        return r

    # ---------- the actual deep replay ----------

    def run(self, start_url: str, *, with_iframe: bool = True,
            with_api: bool = True) -> None:
        if not _CURL_CFFI_AVAILABLE:
            self.log.error("curl_cffi is not installed")
            print(f"{C.RED}Run: pip install --break-system-packages curl_cffi certifi{C.RESET}")
            return

        self.sess = self._build_session()
        self.log._meta["url"] = start_url
        self.log._meta["fingerprint"] = self.fp

        self.log.info(f"Starting deep replay")
        self.log.info(f"Target: {start_url}")
        self.log.info(f"Fingerprint: {self.fp}")
        if _CERTIFI_PATH:
            self.log.info(f"CA bundle: {_CERTIFI_PATH}")
        self.log.info("─" * 78)

        # ---- Phase 1: the main page (with redirects) ----
        self.log.info("PHASE 1: main page")
        r = self._do("GET", start_url)
        if r is None:
            self.log.complete("aborting: main page failed")
            return

        # Parse for sub-resources
        ct = ""
        for k, v in (r.headers.items() if hasattr(r.headers, "items") else []):
            if k.lower() == "content-type":
                ct = v
                break
        if "html" in ct.lower():
            html = r.text or ""
            res = extract_resources(html)
            counts = {k: len(v) for k, v in res.items()}
            self.log.info(
                f"  page: {len(html):,} chars, "
                + ", ".join(f"{c} {k}" for k, c in counts.items())
            )
        else:
            res = {"scripts": [], "links": [], "images": [],
                   "iframes": [], "prefetch": [], "preconnect": []}

        # ---- Phase 2: sub-resources ----
        self.log.info("PHASE 2: sub-resources")
        sub_queue: list[tuple[str, str]] = []
        for kind, urls in res.items():
            for u in urls:
                sub_queue.append((kind, u))
        self.log.info(f"  total: {len(sub_queue)}")
        for kind, sub in sub_queue:
            if sub.startswith("//"):
                sub = "https:" + sub
            elif sub.startswith("/"):
                p = urlparse(start_url)
                sub = f"{p.scheme}://{p.netloc}{sub}"
            if sub in self.visited:
                continue
            self.log.info(f"  → {kind}: {sub[:120]}")
            r2 = self._do("GET", sub)
            if r2 is not None:
                cts = (r2.headers.get("Content-Type") or "").split(";")[0]
                self.log.info(f"    ← {r2.status_code} {cts or '?'} "
                              f"{_fmt_size(len(r2.content))}")

        # ---- Phase 3: known API endpoints the page polls ----
        if with_api:
            self.log.info("PHASE 3: known API endpoints")
            for api_url in KNOWN_API_ENDPOINTS:
                # use the cache-bust pattern from the HAR
                sep = "&" if "?" in api_url else "?"
                url = f"{api_url}{sep}__cacheBust={random.random()}"
                if api_url == "https://perchance.org/api/getCommunityData":
                    pass  # already optional, no params
                self.log.info(f"  → {api_url[:100]}")
                r3 = self._do("GET", url)
                if r3 is not None:
                    self.log.info(f"    ← {r3.status_code} "
                                  f"{(r3.headers.get('Content-Type') or '?').split(';')[0]} "
                                  f"{_fmt_size(len(r3.content))}")

        # ---- Phase 4: the embed iframe logic ----
        if with_iframe:
            self.log.info("PHASE 4: embed iframe flow")
            self._replay_iframe_flow()

        # ---- Summary ----
        self.log.info("─" * 78)
        from collections import Counter
        sc_count = Counter(e.status for e in self.log.events
                           if e.kind == "response")
        host_count = Counter(urlparse(e.url).netloc for e in self.log.events
                             if e.kind == "response" and e.url)
        n_total = len(self.log.events)
        n_responses = sum(1 for e in self.log.events if e.kind == "response")
        n_errors = sum(1 for e in self.log.events if e.kind == "error")
        self.log.complete(
            f"done — {n_total} events, {n_responses} responses, {n_errors} errors"
        )
        if sc_count:
            self.log.complete(
                "  status: " + ", ".join(f"{c}×{s}" for s, c in sorted(sc_count.items()))
            )
        if host_count:
            top = host_count.most_common(5)
            self.log.complete(
                "  top hosts: " + ", ".join(f"{h}×{c}" for h, c in top)
            )

    def _replay_iframe_flow(self) -> None:
        """
        Replay what the embed iframe does in a real browser:
          1. GET /embed
          2. GET /api/getAccessCodeForAdPoweredStuff  (on perchance.org)
          3. GET /api/verifyUser?thread=N  →  userKey
          4. POST /api/generate  (with userKey + adAccessCode + payload)
          5. GET /api/awaitExistingGenerationRequest
          6. GET /api/getUserQueuePosition
          7. GET /api/downloadTemporaryImageViaProxy?t=...
        """
        # 1. The embed page itself
        self.log.info("  → embed page (image-generation.perchance.org/embed)")
        r = self._do("GET", EMBED_URL)
        if r is None:
            self.log.error("embed page failed; skipping iframe flow")
            return

        # 2. adAccessCode (this is the one most likely to be Cloudflare-gated)
        self.log.info("  → adAccessCode")
        ad_code = None
        try:
            r = self._do("GET",
                         f"{AD_CODE_URL}?__cacheBust={random.random()}")
            if r is not None and r.status_code == 200:
                # Body is a 64-hex string in quotes
                body = (r.text or "").strip().strip('"')
                if re.fullmatch(r"[a-f0-9]{64}", body):
                    ad_code = body
                    self.log.info(f"    ← adAccessCode: {ad_code[:16]}…")
        except Exception as e:
            self.log.info(f"    adAccessCode failed: {e}", note="non-fatal")

        # 3. verifyUser — the main one we care about
        self.log.info("  → verifyUser (asking for userKey)")
        user_key = None
        for thread_id in range(3):  # try a few threads
            r = self._do(
                "GET",
                f"{VERIFY_USER_URL}?thread={thread_id}"
                f"&__cacheBust={random.random()}"
            )
            if r is not None and r.status_code == 200:
                body = r.text or ""
                m = re.search(r'"userKey"\s*:\s*"([a-f0-9]{64})"', body)
                if m:
                    user_key = m.group(1)
                    self.log.info(f"    ← userKey: {user_key[:16]}…")
                    break

        if user_key is None:
            self.log.error("could not get userKey; skipping generate")
            return

        # 4. Submit generate
        self.log.info("  → generate")
        request_id = str(random.random())
        body_obj = {
            "prompt": "a cute booy",
            "negativePrompt": "",
            "seed": -1,
            "resolution": "512x768",
            "guidanceScale": 7,
            "channel": "imageapi",
            "subChannel": "public",
            "userKey": user_key,
            "requestId": request_id,
        }
        if ad_code:
            body_obj["adAccessCode"] = ad_code
        url = (f"{GENERATE_URL}?userKey={user_key}"
               f"&requestId={request_id}"
               f"&__cacheBust={random.random()}")
        if ad_code:
            url += f"&adAccessCode={ad_code}"
        r = self._do(
            "POST", url,
            data=json.dumps(body_obj),
            headers={"content-type": "text/plain;charset=UTF-8",
                     "origin": "https://image-generation.perchance.org",
                     "referer": "https://image-generation.perchance.org/embed"},
        )
        if r is None or r.status_code != 200:
            self.log.error("generate request failed")
            return

        # 5. Poll awaitExistingGenerationRequest
        self.log.info("  → poll awaitExistingGenerationRequest (a few times)")
        for i in range(3):
            time.sleep(0.5)
            self._do("GET", f"{AWAIT_URL}?userKey={user_key}"
                              f"&__cacheBust={random.random()}")

        # 6. Poll queue position
        self.log.info("  → poll getUserQueuePosition")
        self._do("GET", f"{QUEUE_POS_URL}?userKey={user_key}"
                          f"&requestId={request_id}"
                          f"&__cacheBust={random.random()}")

        # 7. Try to download (only if the response had a t token)
        body = r.text or ""
        m = re.search(r'"t"\s*:\s*"(v1\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)"', body)
        if m:
            t = m.group(1)
            self.log.info(f"  → downloadTemporaryImageViaProxy (t={t[:30]}…)")
            self._do("GET", f"{DOWNLOAD_VIA_PROXY_URL}?t={t}")


# =============================================================================
# Live HTML thread
# =============================================================================


def _html_refresher(log: EventLog, path: Path, stop: threading.Event) -> None:
    """Periodically rewrite the HTML file so the user sees live updates."""
    while not stop.is_set():
        try:
            log.write_html()
        except Exception:
            pass
        stop.wait(1.0)


# =============================================================================
# CLI
# =============================================================================


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="deep_logger.py",
        description="Deep network logger for the imageapi URL.",
    )
    ap.add_argument("--url", default=DEFAULT_URL,
                    help="URL to crawl (default: imageapi page)")
    ap.add_argument("--prompt", default=None,
                    help="Override the prompt query param")
    ap.add_argument("--out", default="index.html",
                    help="HTML output path (default: index.html)")
    ap.add_argument("--no-html", action="store_true",
                    help="Skip writing the HTML dashboard")
    ap.add_argument("--no-iframe", action="store_true",
                    help="Skip the embed-iframe flow")
    ap.add_argument("--no-api", action="store_true",
                    help="Skip the known API endpoints")
    ap.add_argument("--just-page", action="store_true",
                    help="Only fetch the main page, nothing else")
    ap.add_argument("--no-console", action="store_true",
                    help="Don't print to console (HTML only)")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args(argv[1:])

    # Build the URL with the prompt
    url = args.url
    if args.prompt:
        url = url.split("?")[0] + f"?prompt={args.prompt}"

    log = EventLog(
        html_path=None if args.no_html else Path(args.out),
        console=not args.no_console,
    )
    stop = threading.Event()
    html_thread = None
    if not args.no_html:
        html_thread = threading.Thread(
            target=_html_refresher, args=(log, Path(args.out), stop),
            daemon=True,
        )
        html_thread.start()

    try:
        driver = DeepDriver(log, timeout=args.timeout)
        driver.run(
            url,
            with_iframe=not args.no_iframe and not args.just_page,
            with_api=not args.no_api and not args.just_page,
        )
    finally:
        stop.set()
        if html_thread:
            html_thread.join(timeout=3)
        if not args.no_html:
            log.write_html()
            print(f"\n{C.GREEN}HTML dashboard: {C.BOLD}{args.out}{C.RESET}")
            print(f"  → open in your browser to see the live network log")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
