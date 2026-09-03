#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 main.py — Proxied Chrome (VPN-like) + Continuous Network Logging + spys.one
           proxy scraper  |  ALL IN ONE FILE  |  Python + DrissionPage + curl_cffi
================================================================================

WHAT THIS FILE DOES
-------------------
1. SCRAPES proxies from https://spys.one/free-proxy-list/ALL/ using `curl_cffi`
   (with a full explanation of what is inside that page and how ports are
   hidden), plus dependable fallbacks when spys.one shows a Cloudflare
   challenge.
2. STARTS ONE proxied Chrome via DrissionPage with:
       PROXY = "a value here"      <-- you edit this ONE variable
   That Chrome works like a VPN for every tab/navigation: every request the
   browser makes goes through your proxy.
3. COLLECTS ALL network logs continuously and prints them live in the console
   (method, status, type, URL, time) and saves them to
   `network_logs.jsonl` + `network_logs.txt`.
4. KEEPS ONE TAB CONTINUOUSLY WORKING: you type any URL at the prompt
   (the "homepage" is NOT predefined — you control it live) and the same tab
   navigates there while the SAME listener keeps logging across every site
   load without restarting.

QUICK START
-----------
    pip install curl_cffi DrissionPage
    python main.py

Then:
  - If PROXY is still "a value here" (or empty) the program scrapes proxies,
    shows you the top ones, and asks you to paste/pick one.
  - A visible Chrome opens THROUGH that proxy.
  - Type any URL when asked, e.g.  https://example.com
    Type  quit  to exit. Logs are saved automatically.

--------------------------------------------------------------------------------
DEEP DIVE — WHAT IS INSIDE https://spys.one/free-proxy-list/ALL/
--------------------------------------------------------------------------------
I fetched this page myself (both with curl_cffi raw + a rendered browser) and
this is exactly what is in it:

A) PAGE STRUCTURE (rendered, what a human sees)
   Columns per proxy row:
     1. Proxy address:port   e.g. 49.147.102.109:5050
     2. Proxy type           HTTP / HTTPS / SOCKS  (+ vendor in brackets,
                             e.g. (Mikrotik) (Squid))
     3. Anonymity            NOA = non-anonymous (transparent)
                             ANM = anonymous
                             HIA = high-anonymous (elite)
     4. Country (city/region) e.g. PH Bacolod City, TH Bangkok, AE Masdar City
     5. Hostname/ORG         reverse-DNS + organisation, e.g.
                             dsl.49.148.102.109.pldt.net (Philippine Long ...)
     6. Latency (sec)        lower = better, e.g. 8.476
     7. Speed bar            relative to other servers
     8. Uptime               e.g. 25% (3)   — percent + successful checks
     9. Check date (GMT+03)  e.g. 03-sep-2026 21:21 (4 mins ago)

   The header also says e.g. "32599 proxies" and there is a filter <form>
   with these POST fields (this is how you ask for MORE rows per page):
     xpp : servers per page — 0=30, 1=50, 2=100, 3=200, 4=300, 5=500
     xf1 : anonymity — 0=All, 1=ANM&HIA, 2=NOA, 3=ANM, 4=HIA
     xf2 : SSL — 0=All, 1=SSL+, 2=SSL-
     xf4 : port filter — 0=All, 1=3128, 2=8080, 3=80
     xf5 : type — 0=All, 1=HTTP, 2=SOCKS
   Newer spys.one also sends a hidden `xx0` token + cookies, so we reuse the
   GET cookies for the POST (see fetch_spys_via_curlcffi()).

B) WHY curl_cffi ALONE GETS BLOCKED (important!)
   Raw `curl_cffi` GET to spys.one returns:
       HTTP 403 + <title>Just a moment...</title> + Cloudflare managed
       challenge (cf-mitigated: challenge, cRay, _cf_chl_opt, Turnstile).
   curl_cffi fixes TLS/JA3 fingerprinting (it impersonates Chrome), but it
   does NOT execute JavaScript, so it cannot solve the interactive managed
   challenge. A real browser (DrissionPage Chrome) CAN pass it.
   => This file therefore implements BOTH paths:
        Path 1 (fast): curl_cffi + pure-Python port decoder (works whenever
                       Cloudflare lets the request through — e.g. residential
                       IP / cached clearance / low-risk moment).
        Path 2 (dependable): DrissionPage renders the page, Cloudflare is
                       solved by the real browser, ports are already decoded
                       in the DOM — we just read the text.
        Path 3 (fallback): other free lists that DO work with curl_cffi
                       (Geonode, ProxyScrape, TheSpeedX GitHub) so you always
                       get *some* proxies even if spys.one is fully blocked.

C) HOW PORTS ARE HIDDEN (the "deep" part — verified against real HTML)
   Raw HTML does NOT contain the port. It contains:
     <font class="spy14">177.80.196.145<script>document.write(
        "<font class=spy2>:</font>"
        +(Four7ZeroOne^Zero7Five)+(Four6ThreeZero^Two1Two)+ ... )</script></font>
   And earlier in the page ONE packed script:
     <script>eval(function(p,r,o,x,y,s){...}('h=1;f=D^C;e=5;...',60,60,
        '...Zero7Five^Four^Nine^...^11399^5065^443^8080^...'.split('^'),0,{}))</script>
   That is Dean Edwards' JS packer. Unpacked it gives ~30 assignments, e.g.:
     Seven=1; Five=5; Six=0; Four=3; ...
     FourFourFour=769^8909; SevenNineZero=9503^3127; ...
     TwoOneFourFour=Six^Five6Nine; Four6ThreeZero=Seven^Two1Two; ...
   Each `(A^B)` in document.write is a BITWISE XOR of two integers/variables
   and evaluates to ONE decimal digit (0-9). Concatenated they form the port:
     (Four7ZeroOne^Zero7Five)=3, (...)=1, (...)=2, (...)=8  =>  port "3128"
     5 terms => 5-digit port like "53281"; 4 terms => "8080", etc.
   Modern spys.one uses the SAME trick with shorter names like
     (N5FS ^ ONE5N)+(NETZ ^ Z5O)+...
   So the decoder in this file is GENERIC: unpack any packer, build the
   variable map iteratively, then evaluate every (A^B) term to a digit.

D) HOW TO USE THE DATA
   - Free spys.one proxies are UNAUTHENTICATED `IP:PORT` (no user/pass).
   - They die fast. Always test before use (test_proxy() does
     `https://api.ipify.org` through the proxy with curl_cffi).
   - For Chrome: `--proxy-server=http://IP:PORT` routes EVERYTHING via proxy
     (that is the "VPN for the tab" effect you asked for).
   - For curl_cffi: proxies={"http": "http://IP:PORT", "https": ...}.
   - Prefer HIA/ANM + low latency + high uptime% + recent check date.

================================================================================
"""

# =============================================================================
# 0. CONFIG — EDIT THIS
# =============================================================================

# >>> PUT YOUR PROXY HERE <<<
# Accepted formats (all work):
#   "123.45.67.89:8080"                 (assumed http)
#   "http://123.45.67.89:8080"
#   "https://123.45.67.89:8080"
#   "socks5://123.45.67.89:1080"        (Chrome supports it, curl_cffi too)
#   "http://user:pass@123.45.67.89:8080" (note: Chrome/DrissionPage will pop
#                                         an auth dialog for user:pass proxies;
#                                         prefer auth-free spys.one proxies)
PROXY = "a value here"

# spys.one page to scrape
SPYS_URL = "https://spys.one/free-proxy-list/ALL/"

# Browser behaviour
HEADLESS = False          # False = you SEE the Chrome and can type/click in it.
                          # True  = headless (for servers / scraping only).
HOMEPAGE = "https://example.com"   # default first page; you can type ANY other
                                   # URL at the prompt afterwards.

# Logging
LOG_JSONL = "network_logs.jsonl"   # one JSON object per request (machine-readable)
LOG_TXT = "network_logs.txt"       # human-readable lines
CLEAR_LOGS_ON_START = True         # True = fresh logs each run

# Scraper behaviour
WANT_PER_PAGE = 500       # ask spys.one for 500 rows via POST xpp=5 (if allowed)
SHOW_TOP_N = 15           # how many scraped proxies to preview in console
TEST_PROXY_BEFORE_LAUNCH = True    # quick curl_cffi check via api.ipify.org
                                    # (skipped when the parallel auto-test below
                                    # already verified the proxy)

# Parallel proxy tester (used when PROXY is still the placeholder)
PARALLEL_TEST_WORKERS = 100   # threads checking proxies at the same time
PARALLEL_TEST_TIMEOUT = 8     # seconds per proxy check (low = fast)
PARALLEL_TEST_LIMIT = 500     # max scraped proxies to test (covers ALL 500)
AUTO_USE_FASTEST = True       # True = auto-launch Chrome with the fastest
                              # working proxy, no more prompts. False = show
                              # the working list and let you pick.
PROXY_TEST_URL = "https://api.ipify.org?format=json"

# =============================================================================
# 1. IMPORTS
# =============================================================================
import json
import re
import sys
import time
import queue
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("[FATAL] Missing dependency: curl_cffi. Install with:")
    print("        pip install curl_cffi")
    sys.exit(1)

# DrissionPage is imported lazily (only needed for the browser part) so that
# the scraper/test helpers can still run even if the browser is unavailable.


# =============================================================================
# 2. SMALL UTILITIES
# =============================================================================
def ts():
    """Current time string for console lines."""
    return datetime.now().strftime("%H:%M:%S")


def log_print(*args):
    print(f"[{ts()}]", *args, flush=True)


def looks_like_placeholder(proxy: str) -> bool:
    p = (proxy or "").strip().lower()
    return (not p) or p in ("a value here", "a_value_here", "value here",
                            "proxy", "none", "null") or "value here" in p


def ensure_url_scheme(u: str) -> str:
    """Make sure a URL has http(s):// for tab.get()."""
    u = (u or "").strip()
    if not u:
        return ""
    if u.lower() in ("quit", "exit", "q"):
        return u
    if not re.match(r"^https?://", u, re.I):
        return "https://" + u
    return u


# =============================================================================
# 3. PROXY NORMALISATION  (one string -> Chrome form + curl_cffi form)
# =============================================================================
def normalize_proxy(proxy: str):
    """
    Returns dict with:
      original, chrome (for --proxy-server), cffi (dict for curl_cffi),
      bare (host:port), scheme, has_auth
    Raises ValueError if unparseable.
    """
    raw = (proxy or "").strip()
    if looks_like_placeholder(raw):
        raise ValueError("PROXY is still the placeholder. Paste a real proxy.")
    # allow bare "host:port"
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    port = parsed.port
    user = parsed.username
    pw = parsed.password
    if not host or not port:
        raise ValueError(f"Cannot parse proxy {proxy!r}. Use host:port e.g. 1.2.3.4:8080")
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        # allow hostnames too
        pass
    if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
        scheme = "http"
    bare = f"{host}:{port}"
    auth_prefix = f"{user}:{pw}@" if user else ""
    full = f"{scheme}://{auth_prefix}{bare}"
    # Chrome wants scheme://host:port (auth inside URL triggers a login popup;
    # DrissionPage itself warns UNSUPPORTED_USER_PROXY for that case).
    chrome = full
    cffi = {"http": full, "https": full}
    return {"original": proxy.strip(), "chrome": chrome, "cffi": cffi,
            "bare": bare, "scheme": scheme, "has_auth": bool(user),
            "host": host, "port": port, "user": user}


# =============================================================================
# 4. spys.one DECODER  (pure python — no JS engine needed)
# =============================================================================
def _base_n_token(n: int, base: int) -> str:
    """Replicate the packer's y(c): base-`base` token used for word mapping."""
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"

    def rec(c):
        prefix = "" if c < base else rec(c // base)
        c2 = c % base
        suffix = chr(c2 + 29) if c2 > 35 else chars[c2]
        return prefix + suffix

    return rec(n)


def _unpack_packer_block(p_str: str, base: int, words: list) -> str:
    """Unpack ONE Dean-Edwards-packed block: replace tokens back to words."""
    decoded = p_str
    for idx in range(len(words) - 1, -1, -1):
        w = words[idx]
        if not w:
            continue
        token = _base_n_token(idx, base)
        try:
            decoded = re.sub(r"\b" + re.escape(token) + r"\b", w, decoded)
        except re.error:
            decoded = decoded.replace(token, w)
    return decoded


def unpack_all_packed_scripts(html: str) -> str:
    """
    Find every  ('...',base,count,'...'.split(...))  packer payload in the HTML
    and unpack the ones that look like spys variable definitions
    (contain '=' and '^' and ';'). Returns concatenated decoded JS.
    """
    out = []
    for m in re.finditer(r"\('([^']+)',(\d+),(\d+),'([^']*)'\.split\(", html, re.S):
        p_str, base, _count, x_str = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        # Heuristic: spys payload is assignments with XORs
        if ("=" in p_str and "^" in p_str and ";" in p_str) or ("=" in x_str and "^" in x_str):
            try:
                # spys.one always joins the word table with '^' (written in
                # source as '^' or as '\u005e' — same character), so the
                # captured table itself is '^'-separated.
                words = x_str.split("^")
                out.append(_unpack_packer_block(p_str, base, words))
            except Exception:
                continue
    # Fallback: also handle the common exact form split('\u005e')
    if not out:
        for m in re.finditer(r"\('([^']+)',(\d+),(\d+),'([^']*)'\.split\('\\u005e'\)", html, re.S):
            p_str, base, x_str = m.group(1), int(m.group(2)), m.group(4)
            if "=" in p_str and "^" in p_str:
                out.append(_unpack_packer_block(p_str, base, x_str.split("^")))
    return "\n".join(out)


def build_var_map(js_text: str, html_fallback: str = "") -> dict:
    """
    Build {VAR: int} from JS like  `Seven=1; FourFourFour=769^8909; ...`
    Works for both old word-names and new names like N5FS/ONE5N.
    Also scans raw <script> bodies for plain `var A = 123` / `A=1^2` defs.
    """
    # Ignore the packer boilerplate itself (it contains '=' and '^' but no
    # real variables, e.g. "c<r", "o--", "y=function...").
    clean_fallback = "\n".join(
        s for s in html_fallback.split("\n")
        if "y=function" not in s and "parseInt(c/r)" not in s
    ) if html_fallback else ""
    combined = (js_text or "") + "\n" + clean_fallback
    # collect assignments: NAME = NUMBER  or  NAME = XXX ^ YYY
    # (operands may be variable names OR numeric literals like 769^8909)
    assigns = re.findall(
        r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*([A-Za-z0-9_]+\s*\^\s*[A-Za-z0-9_]+|\d+)",
        combined,
    )
    env: dict = {}
    pending = list(assigns)
    # iterate until fixpoint (handles forward references)
    for _ in range(20):
        if not pending:
            break
        progress = False
        rest = []
        for name, expr in pending:
            expr = expr.strip()
            if re.fullmatch(r"\d+", expr):
                if name not in env:
                    env[name] = int(expr)
                    progress = True
                continue
            parts = [p.strip() for p in expr.split("^")]
            if len(parts) != 2:
                continue
            a, b = parts

            def resolve(tok):
                if re.fullmatch(r"\d+", tok):
                    return int(tok)
                return env.get(tok)

            av, bv = resolve(a), resolve(b)
            if av is not None and bv is not None:
                if name not in env:
                    env[name] = av ^ bv
                    progress = True
            else:
                rest.append((name, expr))
        pending = rest
        if not progress:
            break
    return env


def _eval_port_terms(script_body: str, env: dict):
    """Evaluate every (A^B) term in a document.write body -> port string."""
    terms = re.findall(r"([A-Za-z][A-Za-z0-9_]*\s*\^\s*[A-Za-z0-9_]+)", script_body)
    digits = []
    for t in terms:
        a, b = [x.strip() for x in t.split("^")]

        def resolve(tok):
            if re.fullmatch(r"\d+", tok):
                return int(tok)
            return env.get(tok)

        av, bv = resolve(a), resolve(b)
        if av is None or bv is None:
            return None  # unknown vars -> caller skips / falls back
        v = av ^ bv
        if not (0 <= v <= 9):
            # spys ports are single digits per term; anything else means our
            # var map is stale for this page -> signal failure
            return None
        digits.append(str(v))
    return "".join(digits) if digits else None


def is_cloudflare_challenge(html: str, status: int) -> bool:
    t = (html or "")[:8000].lower()
    return (status == 403 and ("just a moment" in t or "challenge-platform" in t
                               or "_cf_chl_opt" in t or "cf-mitigated" in t)) \
        or ("just a moment" in t and "enable javascript and cookies" in t)


def decode_spys_html(html: str):
    """
    Full pipeline: HTML -> list of proxies.
    Returns (proxies, info) where proxies = [{ip, port, address, ...}]
    and info describes which method worked.
    """
    # 1. unpack packer(s)
    decoded_js = unpack_all_packed_scripts(html)
    # 2. also grab plain script bodies as fallback var source (for modern
    #    spys.one variants that define vars without a packer). Skip the
    #    packer boilerplate scripts themselves.
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S | re.I)
    plain_scripts = [s for s in scripts
                     if ("=" in s)
                     and ("eval(function" not in s)
                     and ("y=function" not in s)]
    plain_js = "\n".join(plain_scripts)
    env = build_var_map(decoded_js, plain_js)

    # 3. pair each IP with its following <script>document.write...</script>
    pairs = re.findall(
        r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*<script[^>]*>(.*?)</script>",
        html, re.S | re.I,
    )
    proxies = []
    for ip, body in pairs:
        if "document.write" not in body or "^" not in body:
            continue
        port = _eval_port_terms(body, env) if env else None
        if not port:
            continue
        proxies.append({"ip": ip.strip(), "port": port,
                        "address": f"{ip.strip()}:{port}",
                        "source": "spys.one"})
    info = {"decoded_vars": len(env), "ip_script_pairs": len(pairs),
            "decoded_proxies": len(proxies),
            "used_packer": bool(decoded_js)}
    return proxies, info


def enrich_spys_rows(html: str, proxies: list) -> list:
    """
    Best-effort: attach type/anonymity/country/org/latency/uptime/checkdate
    from the <tr class="spy1x..."> rows (rendered text, ports stripped).
    If parsing fails we simply return proxies unchanged.
    """
    try:
        # Split rows; first cell holds IP, rest hold metadata as plain text.
        rows = re.findall(r'<tr class="?spy1x+x?"?[^>]*>(.*?)</tr>', html, re.S | re.I)
        # Map ip -> row text
        by_ip = {}
        for r in rows:
            ipm = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", r)
            if not ipm:
                continue
            # strip scripts/styles/tags -> text cells
            no_script = re.sub(r"<script.*?</script>", " ", r, flags=re.S | re.I)
            cells = re.findall(r"<td[^>]*>(.*?)</td>", no_script, re.S | re.I)
            texts = []
            for c in cells:
                t = re.sub(r"<[^>]+>", " ", c)
                t = re.sub(r"\s+", " ", t).strip()
                texts.append(t)
            by_ip[ipm.group(1)] = texts
        for p in proxies:
            texts = by_ip.get(p["ip"])
            if not texts:
                continue
            # texts[0] is like "177.80.196.145 :3128" or with port missing
            if len(texts) > 1:
                p["type"] = texts[1][:40]
            if len(texts) > 2:
                p["anonymity"] = texts[2][:10]
            if len(texts) > 3:
                p["country"] = texts[3][:80]
            if len(texts) > 4:
                p["org"] = texts[4][:120]
            if len(texts) > 5:
                p["latency"] = texts[5][:20]
            if len(texts) > 7:
                p["uptime"] = texts[7][:40]
            if len(texts) > 8:
                p["checked"] = texts[8][:60]
    except Exception:
        pass
    return proxies


# =============================================================================
# 5. FETCH spys.one WITH curl_cffi  (fast path — shows what's inside)
# =============================================================================
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.google.com/",
}

SPYS_POST_FORM = {  # xpp=5 -> 500 rows; xf1=0 all; xf4=0 all ports; xf5=0 all types
    "xpp": "5", "xf1": "0", "xf2": "0", "xf4": "0", "xf5": "0",
}


def fetch_spys_via_curlcffi(url: str = SPYS_URL, want_per_page: int = 500,
                            timeout: int = 30):
    """
    Try spys.one with curl_cffi (Chrome TLS impersonation).
    Steps: GET (cookies) -> optional POST xpp=5 (more rows) -> decode ports.
    Returns dict: {ok, proxies, info, note, raw_len, method}
    ok=False with note='cloudflare-challenge' when CF blocks us — caller then
    falls back to the real browser or to other free sources.
    """
    sess = cffi_requests.Session(impersonate="chrome124")
    # --- GET ---
    try:
        r = sess.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        html = r.text
    except Exception as e:
        return {"ok": False, "proxies": [], "info": {},
                "note": f"curl_cffi GET failed: {e}", "method": "curl_cffi/GET"}
    if is_cloudflare_challenge(html, r.status_code):
        Path("spys_cf_block.html").write_text(html, encoding="utf-8",
                                              errors="ignore")
        return {"ok": False, "proxies": [], "info": {},
                "note": ("cloudflare-challenge: spys.one returned 403 "
                         "'Just a moment'. curl_cffi cannot solve JS "
                         "challenges. Raw saved to spys_cf_block.html — "
                         "use the DrissionPage fallback in this file."),
                "method": "curl_cffi/GET", "status": r.status_code}
    proxies, info = decode_spys_html(html)
    proxies = enrich_spys_rows(html, proxies)
    method = "curl_cffi/GET"
    # --- POST for more rows (only if GET decoded fine) ---
    if want_per_page >= 100 and r.status_code == 200:
        try:
            form = dict(SPYS_POST_FORM)
            # map want_per_page -> xpp value
            mapping = [(500, "5"), (300, "4"), (200, "3"),
                       (100, "2"), (50, "1")]
            for need, val in mapping:
                if want_per_page >= need:
                    form["xpp"] = val
                    break
            else:
                form["xpp"] = "0"
            rp = sess.post(url, data=form, headers={**BROWSER_HEADERS,
                           "Referer": url,
                           "Content-Type": "application/x-www-form-urlencoded",
                           "Origin": "https://spys.one"},
                           timeout=timeout)
            if not is_cloudflare_challenge(rp.text, rp.status_code) \
                    and rp.status_code == 200:
                p2, i2 = decode_spys_html(rp.text)
                if len(p2) > len(proxies):
                    proxies, info = enrich_spys_rows(rp.text, p2), i2
                    method = "curl_cffi/GET+POST(xpp=%s)" % form["xpp"]
                    html = rp.text
        except Exception as e:
            info["post_warning"] = str(e)
    Path("spys_last.html").write_text(html, encoding="utf-8", errors="ignore")
    if not proxies:
        return {"ok": False, "proxies": [], "info": info,
                "note": ("downloaded but decoded 0 proxies — spys.one likely "
                         "changed its obfuscation. Use browser fallback."),
                "method": method}
    return {"ok": True, "proxies": proxies, "info": info,
            "note": f"decoded {len(proxies)} via {method}",
            "method": method}


# =============================================================================
# 6. FALLBACK FREE PROXIES VIA curl_cffi (these have NO Cloudflare)
# =============================================================================
def fetch_fallback_proxies(limit: int = 100, timeout: int = 25):
    """
    Geonode + ProxyScrape + TheSpeedX — all verified to work with curl_cffi.
    Returns list of {ip, port, address, source}.
    """
    out, notes = [], []
    sess = cffi_requests.Session(impersonate="chrome124")
    # 1) ProxyScrape plain text
    try:
        u = ("https://api.proxyscrape.com/v2/?request=getproxies&protocol=http"
             "&timeout=10000&country=all&ssl=all&anonymity=all")
        r = sess.get(u, timeout=timeout)
        if r.status_code == 200:
            for line in r.text.splitlines():
                line = line.strip()
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", line):
                    ip, port = line.split(":")
                    out.append({"ip": ip, "port": port, "address": line,
                                "source": "proxyscrape"})
            notes.append(f"proxyscrape:{len([p for p in out if p['source']=='proxyscrape'])}")
    except Exception as e:
        notes.append(f"proxyscrape-err:{e}")
    # 2) Geonode JSON
    try:
        u = ("https://proxylist.geonode.com/api/proxy-list?limit=%d&page=1"
             "&sort_by=lastChecked&sort_type=desc" % min(limit, 500))
        r = sess.get(u, timeout=timeout)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                ip, port = item.get("ip"), str(item.get("port", ""))
                if ip and port.isdigit():
                    out.append({"ip": ip, "port": port,
                                "address": f"{ip}:{port}",
                                "source": "geonode",
                                "country": item.get("country"),
                                "anonymity": item.get("anonymityLevel"),
                                "latency": item.get("latency")})
            notes.append("geonode:ok")
    except Exception as e:
        notes.append(f"geonode-err:{e}")
    # 3) TheSpeedX GitHub list
    try:
        u = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"
        r = sess.get(u, timeout=timeout)
        if r.status_code == 200:
            for line in r.text.splitlines():
                line = line.strip()
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", line):
                    ip, port = line.split(":")
                    if all(p["address"] != line for p in out):
                        out.append({"ip": ip, "port": port, "address": line,
                                    "source": "thespeedx"})
            notes.append("thespeedx:ok")
    except Exception as e:
        notes.append(f"thespeedx-err:{e}")
    # de-dup, cap
    seen, uniq = set(), []
    for p in out:
        if p["address"] not in seen:
            seen.add(p["address"])
            uniq.append(p)
        if len(uniq) >= limit:
            break
    return uniq, notes


# =============================================================================
# 7. TEST A PROXY WITH curl_cffi (before launching Chrome)
# =============================================================================
def test_proxy(proxy: str, timeout: int = 15):
    """
    GET https://api.ipify.org?format=json through the proxy with curl_cffi.
    Returns (ok, detail_string).
    """
    try:
        norm = normalize_proxy(proxy)
    except ValueError as e:
        return False, str(e)
    try:
        r = cffi_requests.get("https://api.ipify.org?format=json",
                              proxies=norm["cffi"],
                              impersonate="chrome124", timeout=timeout)
        if r.status_code == 200:
            try:
                return True, f"exit-IP {r.json().get('ip')} via {norm['bare']}"
            except Exception:
                return True, f"HTTP 200 via {norm['bare']}: {r.text[:80]}"
        return False, f"HTTP {r.status_code} via {norm['bare']}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


# =============================================================================
# 7b. PARALLEL PROXY TESTER — tries ALL scraped proxies at once, keeps winners
# =============================================================================
def test_one_proxy_fast(address: str, timeout: int = 8,
                        test_url: str = None):
    """
    One fast liveness check: GET test_url through `address` with curl_cffi.
    Returns dict {address, ok, latency, detail}. Thread-safe (own request).
    """
    test_url = test_url or PROXY_TEST_URL
    addr = address.strip()
    if "://" not in addr:
        addr = "http://" + addr
    px = {"http": addr, "https": addr}
    t0 = time.perf_counter()
    try:
        r = cffi_requests.get(test_url, proxies=px,
                              impersonate="chrome124", timeout=timeout)
        dt = time.perf_counter() - t0
        if r.status_code == 200:
            try:
                detail = f"exit-IP {r.json().get('ip')}"
            except Exception:
                detail = f"HTTP 200 ({r.text[:40]})"
            return {"address": address.strip(), "ok": True,
                    "latency": dt, "detail": detail}
        return {"address": address.strip(), "ok": False,
                "latency": dt, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"address": address.strip(), "ok": False,
                "latency": time.perf_counter() - t0,
                "detail": f"{type(e).__name__}: {str(e)[:100]}"}


def test_proxies_parallel(proxies, max_workers: int = None,
                          timeout: int = None, limit: int = None,
                          test_url: str = None):
    """
    Test EVERY proxy in `proxies` SIMULTANEOUSLY (ThreadPoolExecutor) and
    return the WORKING ones sorted fastest-first.
    Each entry = original proxy dict + test_latency + test_detail.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = max_workers or PARALLEL_TEST_WORKERS
    timeout = timeout or PARALLEL_TEST_TIMEOUT
    limit = PARALLEL_TEST_LIMIT if limit is None else limit
    # de-dup, preserve order
    todo, seen = [], set()
    for p in proxies:
        a = (p["address"] if isinstance(p, dict) else str(p)).strip()
        if a and a not in seen:
            seen.add(a)
            todo.append((a, p))
        if limit and len(todo) >= limit:
            break
    total = len(todo)
    if not total:
        return []
    log_print(f"Parallel-testing {total} proxies "
              f"({max_workers} threads x {timeout}s timeout) — fastest wins…")
    working, done = [], 0
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(max_workers, total)) as ex:
        futs = {ex.submit(test_one_proxy_fast, a, timeout, test_url): (a, p)
                for a, p in todo}
        for fut in as_completed(futs):
            done += 1
            a, p = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # never crash the sweep on one bad result
                res = {"address": a, "ok": False, "latency": 0,
                       "detail": f"Harness:{e}"}
            if res["ok"]:
                entry = dict(p) if isinstance(p, dict) else {
                    "ip": a, "port": "", "address": a, "source": "?"}
                entry["test_latency"] = round(res["latency"], 2)
                entry["test_detail"] = res["detail"]
                working.append(entry)
                log_print(f"  [OK {done}/{total}] {a} "
                          f"({res['latency']:.2f}s, {res['detail']}) — "
                          f"working: {len(working)}")
            elif done % 25 == 0 or done == total:
                log_print(f"  …checked {done}/{total}, "
                          f"working so far: {len(working)}")
    working.sort(key=lambda e: e.get("test_latency", 9999))
    log_print(f"Parallel test finished in {time.perf_counter() - t0:.1f}s: "
              f"{len(working)}/{total} proxies WORKING.")
    return working


def pick_from_working(working: list) -> str:
    """Show working (fastest-first) proxies; Enter = fastest, or pick/paste."""
    print("\n--- WORKING proxies (fastest first — already verified live) ---")
    for i, p in enumerate(working[:SHOW_TOP_N], 1):
        extra = ""
        if p.get("anonymity"):
            extra += f" {p['anonymity']}"
        if p.get("country"):
            extra += f" {str(p['country'])[:28]}"
        print(f"  [{i:2d}] {p['address']:22s} {p.get('test_latency', '?')}s "
              f"{p.get('test_detail', '')}{extra}")
    print("  [ 0] paste my own proxy instead")
    try:
        choice = input("Press ENTER for fastest, or pick/paste > ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""
    if not choice:
        return working[0]["address"]
    if re.match(r"^\d+$", choice):
        idx = int(choice)
        if 1 <= idx <= min(len(working), SHOW_TOP_N):
            return working[idx - 1]["address"]
        if idx == 0:
            return input("Paste proxy (host:port) > ").strip()
    return choice  # user pasted a proxy directly


# =============================================================================
# 8. PROXIED CHROME WITH DrissionPage  +  CONTINUOUS NETWORK LOGGER
# =============================================================================
def launch_proxied_chrome(proxy: str, headless: bool = False):
    """
    Launch ONE Chrome where EVERY request goes through `proxy`
    (the 'VPN for the tab' effect). Returns (page, browser_options_note).
    Listener is NOT started here — start_continuous_logger() does that once,
    and it then survives every later navigation (same tab keeps logging).
    """
    from DrissionPage import ChromiumOptions, ChromiumPage
    norm = normalize_proxy(proxy)
    co = ChromiumOptions()
    co.headless(headless)
    # dependable flags for proxied / container / sandbox environments
    for arg in ("--no-sandbox", "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run", "--no-default-browser-check"):
        try:
            co.set_argument(arg)
        except Exception:
            pass
    co.set_proxy(norm["chrome"])   # -> --proxy-server=...
    if norm["has_auth"]:
        log_print("NOTE: proxy has user:pass. Chrome will show a login popup — "
                  "type the credentials there. Prefer auth-free proxies.")
    if norm["scheme"].startswith("socks"):
        log_print("NOTE: SOCKS proxy set. DrissionPage prints an upstream warning "
                  "for SOCKS but still passes --proxy-server; it works in Chrome.")
    page = ChromiumPage(co)
    note = f"Chrome launched with --proxy-server={norm['chrome']} headless={headless}"
    return page, note


def scrape_spys_via_browser(page, url: str = SPYS_URL, wait_sec: int = 8):
    """
    Fallback scraper: load spys.one IN THE REAL BROWSER (passes Cloudflare,
    executes document.write so ports are plain text) and read the table.
    Returns list of {ip,port,address,...}. Works on the ALREADY-OPEN page's
    tab object — no new browser needed.
    """
    import time as _t
    page.get(url)
    page.wait.load_start()
    _t.sleep(wait_sec)  # let Cloudflare + document.write finish
    # If CF challenge still visible, give the user time to solve manually.
    for _ in range(6):
        title = (page.title or "")
        html_head = (page.html or "")[:4000].lower()
        if "just a moment" in title.lower() or "just a moment" in html_head:
            log_print("Cloudflare challenge visible — solve it in the Chrome "
                      "window (checkbox), waiting 10s…")
            _t.sleep(10)
        else:
            break
    rows = []
    try:
        # Each proxy row: <tr class="spy1x(x)">, first cell text = "IP:port"
        trs = page.eles("css:tr.spy1x, tr.spy1xx")
        ipa = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s*:\s*(\d{2,5})")
        for tr in trs:
            try:
                txt = tr.text
            except Exception:
                continue
            m = ipa.search(txt.replace("\n", " "))
            if not m:
                continue
            ip, port = m.group(1), m.group(2)
            # remaining cells for metadata
            try:
                cells = [c.text.replace("\n", " ").strip()
                         for c in tr.eles("css:td")]
            except Exception:
                cells = []
            rows.append({"ip": ip, "port": port, "address": f"{ip}:{port}",
                         "source": "spys.one(browser)",
                         "type": (cells[1] if len(cells) > 1 else "")[:40],
                         "anonymity": (cells[2] if len(cells) > 2 else "")[:10],
                         "country": (cells[3] if len(cells) > 3 else "")[:80],
                         "org": (cells[4] if len(cells) > 4 else "")[:120],
                         "latency": (cells[5] if len(cells) > 5 else "")[:20],
                         "uptime": (cells[7] if len(cells) > 7 else "")[:40],
                         "checked": (cells[8] if len(cells) > 8 else "")[:60]})
    except Exception as e:
        log_print("browser scrape warning:", e)
    # de-dup
    seen, uniq = set(), []
    for p in rows:
        if p["address"] not in seen:
            seen.add(p["address"])
            uniq.append(p)
    return uniq


class NetworkLogger:
    """
    Continuous network listener for ONE DrissionPage tab.

    - start() calls tab.listen.start(targets=True) ONCE and spawns a daemon
      thread draining packets via listen.wait(). Because the listener is
      attached to the tab's target and we NEVER restart it on navigation,
      logging continues seamlessly across EVERY later site load in that tab
      (exactly what you asked: "tab continuously working and keep logging
      on another site load").
    - Every packet is printed live in the console AND appended to JSONL + TXT.
    - Thread-safe counters; save()/summary() at any time.
    """

    def __init__(self, tab, jsonl_path: str = LOG_JSONL,
                 txt_path: str = LOG_TXT, clear: bool = True):
        self.tab = tab
        self.jsonl_path = Path(jsonl_path)
        self.txt_path = Path(txt_path)
        self._stop = threading.Event()
        self._thread = None
        self.count = 0
        self.ok_count = 0
        self.fail_count = 0
        self._lock = threading.Lock()
        if clear:
            try:
                if self.jsonl_path.exists():
                    self.jsonl_path.unlink()
                if self.txt_path.exists():
                    self.txt_path.unlink()
            except Exception:
                pass

    # -- public ----------------------------------------------------------
    def start(self):
        self.tab.listen.start(targets=True)  # True = ALL urls, all methods/types
        self._thread = threading.Thread(target=self._drain_loop,
                                        name="netlog", daemon=True)
        self._thread.start()
        log_print(f"Network logger STARTED on tab {getattr(self.tab,'tab_id','?')} "
                  f"— listening to ALL requests (GET+POST, every resource type).")
        log_print(f"Live console logging ON. Saving to {self.jsonl_path} + {self.txt_path}")

    def stop(self):
        self._stop.set()
        try:
            self.tab.listen.stop()
        except Exception:
            pass

    def summary(self):
        with self._lock:
            return {"total": self.count, "ok": self.ok_count,
                    "failed": self.fail_count,
                    "jsonl": str(self.jsonl_path), "txt": str(self.txt_path)}

    # -- internals -------------------------------------------------------
    def _packet_to_record(self, pkt) -> dict:
        """Safely convert a DrissionPage DataPacket to a plain dict."""
        def safe(fn, default=None):
            try:
                return fn()
            except Exception:
                return default
        url = safe(lambda: pkt.url, "?")
        method = safe(lambda: pkt.method, "?")
        rtype = safe(lambda: pkt.resourceType, "?")
        failed = bool(safe(lambda: pkt.is_failed, False))
        status = safe(lambda: pkt.response.status, None)
        mime = safe(lambda: pkt.response.mimeType, None)
        fail_info = None
        if failed:
            fail_info = safe(lambda: str(pkt.fail_info.errorText
                                         if hasattr(pkt.fail_info, "errorText")
                                         else pkt.fail_info), None)
        return {"time": datetime.now().isoformat(timespec="seconds"),
                "tab": safe(lambda: pkt.tab_id, "?"),
                "method": method, "url": url, "status": status,
                "type": rtype, "mime": mime, "failed": failed,
                "fail_info": fail_info,
                "target": safe(lambda: str(pkt.target), "")}

    def _write_record(self, rec: dict):
        line = (f"{rec['time']} {rec['method']} "
                f"{rec['status'] if rec['status'] is not None else 'FAIL'} "
                f"{rec['type']} {rec['url']}")
        if rec.get("failed") and rec.get("fail_info"):
            line += f"  [fail: {rec['fail_info']}]"
        print(line, flush=True)
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with open(self.txt_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[log-write-err] {e}", flush=True)

    def _drain_loop(self):
        while not self._stop.is_set():
            try:
                pkt = self.tab.listen.wait(count=1, timeout=1)
            except Exception:
                continue
            if pkt is False or pkt is None:
                continue  # timeout tick — loop again, keeps listener alive
            try:
                rec = self._packet_to_record(pkt)
            except Exception as e:
                print(f"[pkt-parse-err] {e}", flush=True)
                continue
            with self._lock:
                self.count += 1
                if rec.get("failed") or rec.get("status") in (None, 0):
                    self.fail_count += 1
                else:
                    self.ok_count += 1
            self._write_record(rec)


# =============================================================================
# 9. INTERACTIVE LOOP — homepage NOT predefined, same tab keeps logging
# =============================================================================
def interactive_loop(page, logger: NetworkLogger):
    """
    You type ANY url at the prompt; the SAME tab navigates there and the SAME
    logger keeps logging — nothing restarts. This satisfies:
      "i can put anything in homepage directly, tab continuously working and
       keep logging on another site load".
    Commands:  quit/exit/q  |  new <url> (new tab)  |  tabs  |  stats  |  help
    """
    print("\n" + "=" * 78)
    print(" BROWSER READY — type any URL and press Enter. Same tab keeps logging.")
    print(" Commands:  quit | new <url> | tabs | stats | help")
    print(" Tip: you can ALSO just use the Chrome window directly (type/click) —")
    print("        the logger still captures everything in the listened tab.")
    print("=" * 78 + "\n")
    while True:
        try:
            raw = input("\nURL (or command) > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting loop…")
            break
        if not raw:
            continue
        low = raw.lower()
        if low in ("quit", "exit", "q"):
            break
        if low == "help":
            print("  Just type a URL, e.g.  https://example.com  or  github.com")
            print("  new <url>  open in a NEW tab (logger stays on first tab)")
            print("  tabs       list open tabs   |  stats  logger counters  |  quit")
            continue
        if low == "stats":
            print(" Logger:", logger.summary())
            try:
                print(" Current tab URL:", page.url)
            except Exception:
                pass
            continue
        if low == "tabs":
            try:
                for i, t in enumerate(page.browser.get_tabs()):
                    print(f"  [{i}] {t.title[:60]!r}  {t.url[:100]}")
            except Exception as e:
                print(" tabs err:", e)
            continue
        if low.startswith("new "):
            url = ensure_url_scheme(raw[4:].strip())
            try:
                page.new_tab(url=url)
                log_print(f"Opened NEW tab: {url} (logger still on first tab)")
            except Exception as e:
                print(" new-tab err:", e)
            continue
        url = ensure_url_scheme(raw)
        try:
            log_print(f"Navigating SAME tab -> {url}")
            t0 = time.time()
            page.get(url)
            # DO NOT touch logger here — it keeps running across the load.
            page.wait.load_start(timeout=15)
            dt = time.time() - t0
            log_print(f"Loaded in ~{dt:.1f}s | title={((page.title or '')[:80])!r} | "
                      f"logger total so far: {logger.summary()['total']}")
        except Exception as e:
            print(f"[nav-err] {type(e).__name__}: {str(e)[:300]}")
            print("The logger is still running — try another URL or check the proxy.")


# =============================================================================
# 10. MAIN — orchestrates scrape -> choose proxy -> launch -> log -> browse
# =============================================================================
def pick_proxy_interactively(scraped: list) -> str:
    """Show top proxies, let user pick by number or paste their own."""
    print("\n--- Top scraped proxies (paste number OR paste your own) ---")
    for i, p in enumerate(scraped[:SHOW_TOP_N], 1):
        extra = ""
        if p.get("anonymity"):
            extra += f" {p['anonymity']}"
        if p.get("country"):
            extra += f" {str(p['country'])[:30]}"
        if p.get("latency"):
            extra += f" lat={p['latency']}"
        print(f"  [{i:2d}] {p['address']:22s} ({p.get('source','?')}){extra}")
    print(f"  [ 0] paste my own proxy instead")
    try:
        choice = input(f"Pick [1-{min(len(scraped), SHOW_TOP_N)}] or paste proxy > ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "1"
    if re.match(r"^\d+$", choice):
        idx = int(choice)
        if 1 <= idx <= min(len(scraped), SHOW_TOP_N):
            return scraped[idx - 1]["address"]
        # 0 or out of range -> paste own
        return input("Paste proxy (host:port) > ").strip()
    return choice  # user pasted a proxy directly


def main():
    print("=" * 78)
    print(" PROXIED CHROME + NETWORK LOGGER + spys.one SCRAPER  (single file)")
    print("=" * 78)

    # ---- Step 1: resolve which proxy to use ----
    proxy = PROXY.strip() if isinstance(PROXY, str) else ""
    scraped = []

    auto_tested = False   # True once the proxy was verified by parallel test
    if looks_like_placeholder(proxy):
        log_print("PROXY is still the placeholder — scraping proxies for you…")
        # 1a) fast path: curl_cffi on spys.one
        log_print(f"Trying spys.one via curl_cffi: {SPYS_URL}")
        res = fetch_spys_via_curlcffi(SPYS_URL, want_per_page=WANT_PER_PAGE)
        log_print("spys via curl_cffi:", res["note"])
        spys_ok = bool(res["ok"])
        if spys_ok:
            scraped = res["proxies"]
            log_print(f"Sample: {', '.join(p['address'] for p in scraped[:5])}")
        else:
            log_print("Reason:", res["note"])
            # 1b) dependable fallbacks via curl_cffi (no Cloudflare there)
            log_print("Fetching fallback free proxies via curl_cffi…")
            fb, notes = fetch_fallback_proxies(limit=200)
            log_print("fallbacks:", "; ".join(notes), f"-> {len(fb)} proxies")
            scraped = fb
        if not scraped:
            print("\n[FATAL] No proxies obtained. Check internet and retry, or set")
            print("PROXY manually at the top of main.py to a working IP:PORT.")
            sys.exit(2)
        # 1c) PARALLEL sweep: try ALL scraped proxies at once, keep winners
        working = test_proxies_parallel(scraped)
        if not working and spys_ok:
            # spys list all dead? sweep the fallback pools too before giving up
            log_print("No working spys.one proxy — sweeping fallback pools…")
            fb, notes = fetch_fallback_proxies(limit=200)
            log_print("fallbacks:", "; ".join(notes), f"-> {len(fb)} proxies")
            working = test_proxies_parallel(fb)
        if working:
            if AUTO_USE_FASTEST:
                proxy = working[0]["address"]
                auto_tested = True
                log_print(f"AUTO-SELECTED fastest working proxy: {proxy} "
                          f"({working[0].get('test_latency')}s, "
                          f"{working[0].get('test_detail')}) — launching Chrome…")
            else:
                proxy = pick_from_working(working)
                # Enter/number = already-verified proxy -> no re-test needed
                if any(proxy == w["address"] for w in working):
                    auto_tested = True
        else:
            print("\n[WARN] 0 working proxies found in the whole sweep.")
            print("Free proxies die fast — paste your own working proxy, or")
            print("re-run later for a fresh list.")
            proxy = pick_proxy_interactively(scraped)  # manual fallback
    else:
        log_print(f"Using PROXY from config: {proxy}")

    # ---- Step 2: validate proxy string + optional live test ----
    try:
        norm = normalize_proxy(proxy)
        log_print(f"Proxy parsed: scheme={norm['scheme']} bare={norm['bare']} "
                  f"auth={'yes' if norm['has_auth'] else 'no'}")
    except ValueError as e:
        print(f"[FATAL] Bad PROXY: {e}")
        sys.exit(2)

    if TEST_PROXY_BEFORE_LAUNCH and not auto_tested:
        log_print("Testing proxy via curl_cffi (api.ipify.org)…")
        ok, detail = test_proxy(proxy)
        log_print(("PROXY OK: " if ok else "PROXY TEST FAILED: ") + detail)
        if not ok:
            print("The proxy may be dead/slow. You can still try it in Chrome,")
            try:
                ans = input("Continue anyway? [Y/n] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "y"
            if ans == "n":
                print("Set PROXY to another value and re-run. Bye.")
                sys.exit(3)

    # ---- Step 3: (optional) browser-based spys scrape info ----
    # We do NOT block on this — the browser we open next can do it on demand.
    # If user wants fresh spys rows AFTER launch, they can navigate the Chrome
    # to SPYS_URL themselves; ports will show decoded (real browser).

    # ---- Step 4: launch proxied Chrome ----
    log_print("Launching proxied Chrome (DrissionPage)…")
    try:
        page, note = launch_proxied_chrome(proxy, headless=HEADLESS)
    except Exception as e:
        print(f"[FATAL] Could not launch Chrome: {type(e).__name__}: {e}")
        print("Fixes: install Chrome, or on Linux set HEADLESS=True, or check proxy.")
        sys.exit(4)
    log_print(note)
    try:
        log_print("Chrome version:", page.browser_version)
    except Exception:
        pass

    # ---- Step 5: start CONTINUOUS logger ONCE ----
    logger = NetworkLogger(page, LOG_JSONL, LOG_TXT,
                           clear=CLEAR_LOGS_ON_START)
    logger.start()

    # ---- Step 6: open homepage (user-changeable, not fixed) ----
    try:
        log_print(f"Opening start page: {HOMEPAGE}  (type any other URL next)")
        page.get(HOMEPAGE)
        page.wait.load_start(timeout=15)
    except Exception as e:
        log_print(f"start-page nav warning: {type(e).__name__}: {str(e)[:200]}")
        log_print("If the page never loads, the proxy is likely dead — "
                  "quit and try another proxy.")

    # ---- Step 7: interactive loop — SAME tab, logger NEVER restarts ----
    try:
        interactive_loop(page, logger)
    finally:
        s = logger.summary()
        log_print(f"Logger summary: {s['total']} requests "
                  f"({s['ok']} ok, {s['failed']} failed).")
        log_print(f"Saved: {s['jsonl']} and {s['txt']}")
        logger.stop()
        try:
            page.quit()
        except Exception:
            pass
        log_print("Browser closed. Bye!")


if __name__ == "__main__":
    main()
