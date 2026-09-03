"""
turnstile_solver.py — Wrapper around EzSolver (or any compatible
Cloudflare Turnstile HTTP solver service).

EzSolver: https://github.com/ismoiloffS/EzSolver
- Runs a real Chrome (via nodriver) to solve Turnstile
- Exposes a local HTTP API at http://127.0.0.1:8191/solve
- POST with {sitekey, siteurl, timeout} → returns {token, elapsed}

This module is a thin client. It:
  1. Discovers the Turnstile sitekey in a page (3 common patterns)
  2. Calls the EzSolver HTTP service
  3. Returns the token so deep_logger.py can retry verifyUser

Setup (one-time):
  git clone https://github.com/ismoiloffS/EzSolver
  cd EzSolver
  pip install nodriver
  # In one terminal, start the service:
  python service.py        # listens on 0.0.0.0:8191
  # Or set MAX_WORKERS=8 python service.py for more parallelism

  # In another terminal, run deep_logger.py:
  python deep_logger.py --use-turnstile-solver
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

# Where to find the EzSolver service. Override with TURNSTILE_SOLVER_URL.
SOLVER_URL = os.environ.get("TURNSTILE_SOLVER_URL", "http://127.0.0.1:8191/solve")
SOLVER_HEALTH = os.environ.get(
    "TURNSTILE_SOLVER_HEALTH", SOLVER_URL.replace("/solve", "/health")
)

# Common patterns where the Turnstile sitekey appears in HTML/JS.
# Real sitekeys look like: 0x4AAAAAAABBBBBCCCC (18-20 chars total).
# Use {6,} to allow test strings.
SITEKEY_PATTERNS = [
    # <div class="cf-turnstile" data-sitekey="0x...">
    re.compile(r'data-sitekey=["\'](0x[0-9a-fA-F]{6,})["\']'),
    # turnstile.render('selector', {sitekey: '0x...'}) or sitekey=0x...
    re.compile(r'sitekey["\']?\s*[:=]\s*["\'](0x[0-9a-fA-F]{6,})["\']'),
    # ?sitekey=0x... in a script src (no quotes around value)
    re.compile(r'sitekey=(0x[0-9a-fA-F]{6,})'),
    # Just any quoted 0x... in turnstile context
    re.compile(r'["\'](0x[0-9a-fA-F]{6,})["\']'),
]


def find_sitekey(html: str) -> Optional[str]:
    """Scan HTML/JS for a Cloudflare Turnstile sitekey.
    Returns the first match, or None if not found."""
    for pat in SITEKEY_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


def is_solver_available() -> bool:
    """Check if the EzSolver service is up by hitting /health."""
    try:
        req = urllib.request.Request(SOLVER_HEALTH, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def solve(sitekey: str, siteurl: str, timeout: int = 45) -> tuple[str, float]:
    """Call the solver service and return (token, elapsed_seconds)."""
    payload = json.dumps({
        "sitekey": sitekey,
        "siteurl": siteurl,
        "timeout": timeout,
    }).encode()
    req = urllib.request.Request(
        SOLVER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            data = json.loads(body)
        except Exception:
            data = {"error": f"HTTP {e.code}: {body[:200]}"}
        raise RuntimeError(data.get("error", f"HTTP {e.code}"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach solver at {SOLVER_URL}: {e.reason}")
    except Exception as e:
        raise RuntimeError(f"Solver request failed: {e}")

    if "error" in data:
        raise RuntimeError(data["error"])
    if "token" not in data:
        raise RuntimeError(f"Unexpected response: {data}")

    return data["token"], float(data.get("elapsed", 0))


def solve_with_retry(sitekey: str, siteurl: str, *, max_attempts: int = 3,
                     timeout: int = 45) -> Optional[str]:
    """Solve with retries. Returns the token, or None on total failure."""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        try:
            token, elapsed = solve(sitekey, siteurl, timeout=timeout)
            print(f"[solver] got token in {elapsed:.1f}s (attempt {attempt}/{max_attempts})")
            return token
        except Exception as e:
            last_err = e
            print(f"[solver] attempt {attempt}/{max_attempts} failed: {e}")
    print(f"[solver] gave up after {max_attempts} attempts: {last_err}")
    return None


# =============================================================================
# Helper: extract the embed page's HTML, find sitekey, solve.
# Use this when curl_cffi hits token_required and we need a fresh token.
# =============================================================================

def fetch_embed_html_and_sitekey(sess, embed_url: str) -> Optional[str]:
    """Fetch the embed page HTML via the given curl_cffi session and
    extract the Turnstile sitekey. Returns the sitekey or None."""
    try:
        r = sess.get(embed_url, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return None
        return find_sitekey(r.text or "")
    except Exception:
        return None
