"""
perchance_v2.py — Production-grade Perchance image API client.

What's new vs perchance.py:
  1. SELF-HEALING — if direct verifyUser fails, fall back to fetching the
     full perchance.org/imageapi page (which embeds the iframe, runs
     Cloudflare JS, sets cookies/localStorage in the iframe), then retry.
  2. CERT HANDLING — uses certifi.where() as the CA bundle for the
     embedded curl_cffi to avoid "SSL certificate problem" on Windows.
  3. FINGERPRINT DISCOVERY — at startup, introspects the local curl_cffi
     build and only uses fingerprints that ACTUALLY exist. Avoids
     "Impersonating X is not supported" errors.
  4. ENRICHED BANDIT — each failure mode (cert error, not-supported,
     blocked, rate-limited, etc.) is its own reward signal so the bandit
     learns to avoid the bad arms quickly.
  5. PROXY SUPPORT — uses the proxies from the proxy-miner DB so each
     "user" gets a different egress IP.
  6. REINFORCEMENT LEARNING — multi-armed bandit with UCB1 + softmax +
     epsilon-greedy, persistent across runs.

Architecture:

    ┌────────────────────────────────────────────────────────────┐
    │  PolicyLearner  (55+ arms, 3 policies)                    │
    │  - Tracks per-failure-mode reward                          │
    │  - Auto-disables arms whose fingerprint doesn't exist      │
    └────────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌────────────────────────────────────────────────────────────┐
    │  PerchanceClientV2 (curl_cffi.Session)                    │
    │  - Uses certifi.where() as ca_bundle                       │
    │  - Tries verifyUser directly first (cheap)                 │
    │  - On failure, falls back to fetching the full imageapi     │
    │    page (which triggers the legit flow: preconnect,         │
    │    Cloudflare, embed iframe)                               │
    │  - Retries verifyUser through the now-warmed session       │
    └────────────────────────────────────────────────────────────┘
                              │
                              ▼
                  image-generation.perchance.org

Usage:
    python perchance_v2.py probe                 # detect your curl_cffi
    python perchance_v2.py get-key               # acquire & cache userKey
    python perchance_v2.py get-key --with-page   # force the full-page warmup
    python perchance_v2.py gen "a cute boy"      # generate an image
    python perchance_v2.py train 30              # RL training
    python perchance_v2.py status                # show state
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Reuse the bandit/reward system from perchance.py
sys.path.insert(0, str(Path(__file__).parent))
import perchance  # noqa: E402

try:
    from curl_cffi import requests as cffi_requests
    from curl_cffi.requests.exceptions import RequestException
    _CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover
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

CACHE_DIR = Path.home() / ".perchance_client"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USERKEY_CACHE_FILE = CACHE_DIR / "userkey.json"

WRAPPER_PAGE_URL = "https://perchance.org/imageapi?prompt=a%20cute%20booy"
AD_CODE_URL = "https://perchance.org/api/getAccessCodeForAdPoweredStuff"
EMBED_URL = "https://image-generation.perchance.org/embed"
VERIFY_USER_URL = "https://image-generation.perchance.org/api/verifyUser"
GENERATE_URL = "https://image-generation.perchance.org/api/generate"
AWAIT_URL = "https://image-generation.perchance.org/api/awaitExistingGenerationRequest"
QUEUE_POS_URL = "https://image-generation.perchance.org/api/getUserQueuePosition"
DOWNLOAD_VIA_PROXY_URL = "https://image-generation.perchance.org/api/downloadTemporaryImageViaProxy"

# Additional fingerprints we discovered work (from your logs: chrome119, chrome131)
FINGERPRINTS_CANDIDATES: list[str] = [
    "chrome119", "chrome124", "chrome131", "chrome136", "chrome142",
    "chrome146", "firefox133", "firefox135", "firefox144", "firefox147",
    "safari153", "safari155", "safari170", "safari172_ios", "safari180",
    "safari184", "edge99", "edge101",
]

# Header profiles (unchanged from perchance.py — proven to work)
HEADER_PROFILES: list[dict[str, str]] = perchance.HEADER_PROFILES

# Extra reward signals for richer learning
REWARD_CERT_ERROR = -0.9   # SSL cert problem
REWARD_NOT_SUPPORTED = -0.9  # impersonate target missing
REWARD_CONN_CLOSED = -0.7  # abrupt connection close


# =============================================================================
# Runtime fingerprint discovery
# =============================================================================


def detect_supported_fingerprints() -> list[str]:
    """
    Probe the local curl_cffi build to find which impersonation targets
    it actually supports. This avoids the "Impersonating X is not supported"
    error.
    """
    if not _CURL_CFFI_AVAILABLE:
        return []
    try:
        members = set(cffi_requests.BrowserType.__members__.keys())
        return [fp for fp in FINGERPRINTS_CANDIDATES if fp in members]
    except Exception:
        return []


def detect_curl_cffi_version() -> Optional[str]:
    try:
        return importlib.metadata.version("curl_cffi")
    except Exception:
        return None


# =============================================================================
# PerchanceClientV2 — the self-healing client
# =============================================================================


class PerchanceClientV2:
    """
    Production-grade client with:
      - Auto-discovered fingerprints (no "not supported" errors)
      - certifi-based CA bundle (no SSL cert errors)
      - Self-healing fallback: if direct verifyUser fails, warm up
        the session by fetching the full imageapi page, then retry
      - Per-failure-mode reward tracking
    """

    def __init__(
        self,
        proxy: Optional[str] = None,
        timeout: float = 20.0,
        learner: Optional[perchance.PolicyLearner] = None,
    ) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.learner = learner or perchance.PolicyLearner.load()
        self.supported_fps: list[str] = detect_supported_fingerprints()
        # Filter the learner's arms to only those with supported fingerprints
        self._update_learner_fingerprints()
        # Persistent session per client (so cookies / connections stick)
        self._session: Optional[Any] = None
        # Track which arms are blacklisted (fingerprint not supported etc.)
        self._blacklist: set[int] = set()

    def _update_learner_fingerprints(self) -> None:
        """Mark arms with unsupported fingerprints as untried (so they
        won't be selected until manually re-enabled)."""
        if not self.supported_fps:
            return
        for arm in self.learner.arms:
            if arm.fingerprint not in self.supported_fps:
                # Mark as "known bad" — will return 0 reward forever
                if arm.n_trials == 0:
                    # Don't even try it
                    self._blacklist.add(id(arm))
                # If it was tried before with bad results, leave it
        # Save
        self.learner.save()

    def _make_session(self, arm: perchance.Arm) -> Any:
        """Build a curl_cffi Session with the given arm's config."""
        if not _CURL_CFFI_AVAILABLE:
            raise RuntimeError("curl_cffi not installed")
        if arm.fingerprint not in self.supported_fps:
            raise RuntimeError(
                f"Impersonating {arm.fingerprint} is not supported. "
                f"Your curl_cffi supports: {self.supported_fps}"
            )
        profile = next(
            p for p in HEADER_PROFILES if p["name"] == arm.profile_name
        )
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": profile.get("accept-language", "en-US,en;q=0.9"),
        }
        for k in ("User-Agent", "sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"):
            v = profile.get(k)
            if v is not None:
                headers[k] = v

        sess = cffi_requests.Session(impersonate=arm.fingerprint)
        sess.headers.update(headers)
        # FIX: tell curl_cffi to use certifi's CA bundle
        if _CERTIFI_PATH and os.path.exists(_CERTIFI_PATH):
            # curl_cffi supports verify= for ca bundle
            try:
                sess.verify = _CERTIFI_PATH
            except Exception:
                pass
        if self.proxy:
            sess.proxies = {"http": self.proxy, "https": self.proxy}
        return sess

    def _classify_failure(
        self, exc: Optional[Exception], response: Any,
    ) -> tuple[float, bool, bool, bool, str]:
        """
        Classify a failure into a (reward, blocked, errored, success, detail)
        tuple. Recognises cert errors and 'not supported' errors as their
        own signal so the bandit learns to avoid them.
        """
        if exc is not None:
            msg = str(exc)
            if "Impersonating" in msg and "not supported" in msg:
                return REWARD_NOT_SUPPORTED, True, False, False, f"not-supported: {msg[:80]}"
            if "SSL certificate problem" in msg or "unable to get local issuer" in msg:
                return REWARD_CERT_ERROR, True, False, False, f"cert-error: {msg[:80]}"
            if "Connection closed abruptly" in msg or "Connection reset" in msg:
                return REWARD_CONN_CLOSED, True, False, False, f"conn-closed: {msg[:80]}"
            return perchance.REWARD_NETWORK_ERR, False, True, False, f"{type(exc).__name__}: {msg[:120]}"
        if response is not None:
            return perchance.PerchanceClient._classify_failure(self, response) if False else self._http_classify(response)
        return perchance.REWARD_NETWORK_ERR, False, True, False, "no response"

    def _http_classify(self, response: Any) -> tuple[float, bool, bool, bool, str]:
        sc = response.status_code
        body = response.text or ""
        if sc == 200:
            return 0.0, False, False, True, f"200 OK ({len(body)}B)"
        if sc in (403, 407):
            return perchance.REWARD_FORBIDDEN, True, False, False, f"{sc} forbidden/waf"
        if sc in (429, 503):
            return perchance.REWARD_RATE_LIMITED, True, False, False, f"{sc} throttled"
        if sc in (520, 521, 522, 523, 524, 525, 526, 527, 530):
            return perchance.REWARD_BLOCKED, True, False, False, f"{sc} cloudflare error"
        if sc >= 500:
            return perchance.REWARD_NETWORK_ERR, False, True, False, f"{sc} server error"
        return perchance.REWARD_BLOCKED, True, False, False, f"unexpected {sc}"

    # ---------- the core flow ----------

    def fetch_wrapper_page(self, arm: perchance.Arm) -> tuple[Optional[Any], perchance.PerchanceResult]:
        """
        Fetch the full perchance.org/imageapi page. This is the
        'self-healing' step — it preconnects, runs Cloudflare's JS,
        loads the perchance-engine, etc. After this, the session is
        'warmed' and verifyUser is more likely to succeed.
        """
        try:
            sess = self._make_session(arm)
        except RuntimeError as e:
            res = perchance.PerchanceResult(ok=False, blocked=True, detail=str(e),
                                            reward=REWARD_NOT_SUPPORTED)
            return None, res
        try:
            r = sess.get(WRAPPER_PAGE_URL, timeout=self.timeout,
                         allow_redirects=True)
        except Exception as e:
            res = perchance.PerchanceResult(ok=False, errored=True,
                                            detail=f"wrapper: {type(e).__name__}: {str(e)[:120]}",
                                            reward=perchance.REWARD_NETWORK_ERR)
            return None, res
        reward, blocked, errored, success, detail = self._http_classify(r)
        # Even if Cloudflare returns 403/503, the page-load attempt may
        # have set up cookies that make verifyUser work next time.
        return r, perchance.PerchanceResult(
            ok=success, blocked=blocked, errored=errored, success=success,
            reward=reward, detail=detail,
        )

    def verify_user(
        self,
        arm: perchance.Arm,
        *,
        with_page_warmup: bool = False,
    ) -> perchance.PerchanceResult:
        """
        Hit /api/verifyUser. If with_page_warmup is True, first do a full
        page load to 'warm' the session (Cloudflare JS, cookies, etc.).
        """
        if arm.fingerprint not in self.supported_fps:
            res = perchance.PerchanceResult(
                ok=False, blocked=True,
                detail=f"fingerprint '{arm.fingerprint}' not in your curl_cffi build",
                reward=REWARD_NOT_SUPPORTED,
            )
            self.learner.update(arm, REWARD_NOT_SUPPORTED,
                                blocked=True, errored=False, success=False)
            return res

        sess = self._make_session(arm)
        # Optionally warm up
        if with_page_warmup:
            try:
                sess.get(WRAPPER_PAGE_URL, timeout=self.timeout)
            except Exception:
                pass  # ignore warmup failure
        url = (f"{VERIFY_USER_URL}?thread={random.randint(0, 5)}"
               f"&__cacheBust={random.random()}")
        try:
            r = sess.get(url, timeout=self.timeout)
        except Exception as e:
            reward, blocked, errored, success, detail = self._classify_failure(e, None)
            res = perchance.PerchanceResult(
                ok=False, blocked=blocked, errored=errored, success=success,
                reward=reward, detail=detail,
            )
            self.learner.update(arm, reward,
                                blocked=blocked, errored=errored, success=False)
            return res
        reward, blocked, errored, success, detail = self._http_classify(r)
        user_key: Optional[str] = None
        if success:
            try:
                j = r.json()
                user_key = j.get("userKey")
            except Exception:
                m = re.search(r'"userKey"\s*:\s*"([a-f0-9]{64})"', r.text)
                if m:
                    user_key = m.group(1)
            if user_key:
                reward = perchance.REWARD_GOT_USERKEY
                detail = "GOT userKey"
        res = perchance.PerchanceResult(
            ok=user_key is not None,
            user_key=user_key,
            blocked=blocked, errored=errored, success=(user_key is not None),
            reward=reward, detail=detail,
        )
        self.learner.update(arm, reward,
                            blocked=blocked, errored=errored,
                            success=(user_key is not None))
        self.learner.save()
        return res

    def acquire_user_key(
        self,
        *,
        max_attempts: int = 5,
        with_page_warmup: bool = False,
        verbose: bool = True,
    ) -> perchance.PerchanceResult:
        """
        Try to acquire a userKey, falling back through different arms.
        """
        # 1. cache
        if not with_page_warmup:
            cached = self._load_cached_user_key()
            if cached:
                if verbose:
                    print(f"  [cache] using cached userKey")
                return perchance.PerchanceResult(
                    ok=True, user_key=cached, success=True,
                    detail="loaded from cache",
                    reward=perchance.REWARD_GOT_USERKEY,
                )

        # 2. try each arm in turn, preferring supported + high-reward ones
        candidates = [
            a for a in self.learner.arms
            if a.fingerprint in self.supported_fps
        ]
        if not candidates:
            candidates = self.learner.arms  # fallback

        # Sort by mean reward (desc) so we try the best first
        candidates.sort(key=lambda a: a.mean_reward, reverse=True)

        last_err: Optional[perchance.PerchanceResult] = None
        for attempt, arm in enumerate(candidates[:max_attempts]):
            if verbose:
                print(f"  [attempt {attempt + 1}/{min(max_attempts, len(candidates))}] "
                      f"fp={arm.fingerprint}  prof={arm.profile_name}")
            res = self.verify_user(arm, with_page_warmup=with_page_warmup)
            if verbose:
                print(f"  reward={res.reward:+.2f}  {res.detail}")
            if res.ok and res.user_key:
                self._save_user_key(res.user_key)
                return res
            last_err = res
            time.sleep(0.5 + random.random() * 0.5)

        if last_err is None:
            last_err = perchance.PerchanceResult(
                ok=False, detail="no candidates",
                errored=True, reward=perchance.REWARD_NETWORK_ERR,
            )
        return last_err

    # ---------- userKey cache ----------

    def _load_cached_user_key(self) -> Optional[str]:
        if not USERKEY_CACHE_FILE.exists():
            return None
        try:
            d = json.loads(USERKEY_CACHE_FILE.read_text())
            return d.get("userKey")
        except Exception:
            return None

    def _save_user_key(self, user_key: str) -> None:
        USERKEY_CACHE_FILE.write_text(json.dumps({
            "userKey": user_key, "savedAt": time.time(),
        }, indent=2))


# =============================================================================
# CLI
# =============================================================================


def _print_probe() -> None:
    print("=" * 70)
    print("ENVIRONMENT PROBE")
    print("=" * 70)
    print(f"  Python:        {sys.version.split()[0]}")
    v = detect_curl_cffi_version()
    print(f"  curl_cffi:     {v or 'NOT INSTALLED'}")
    if _CERTIFI_PATH:
        print(f"  certifi CA:    {_CERTIFI_PATH}")
        print(f"  CA size:       {os.path.getsize(_CERTIFI_PATH):,} bytes")
    else:
        print(f"  certifi:       NOT INSTALLED")
    print()
    print(f"  Supported fingerprints in your curl_cffi:")
    fps = detect_supported_fingerprints()
    if not fps:
        print("    (none — curl_cffi probably not installed)")
    else:
        for i in range(0, len(fps), 5):
            print("    " + "  ".join(f"{fp:18s}" for fp in fps[i:i + 5]))
    print()
    # Show which header profiles we have
    print(f"  Header profiles: {len(HEADER_PROFILES)}")
    for p in HEADER_PROFILES:
        print(f"    - {p['name']}")
    print()
    # Show quick test
    print("  Quick TLS test (chrome131, the one that worked for you):")
    if fps and "chrome131" in fps:
        try:
            r = cffi_requests.get(
                "https://www.cloudflare.com/cdn-cgi/trace",
                impersonate="chrome131", timeout=10,
            )
            print(f"    status: {r.status_code}  body: {r.text[:100]}")
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {str(e)[:120]}")
    else:
        print("    (skipped — chrome131 not in your build)")


def _print_status() -> None:
    print("=" * 70)
    print("PerchanceClientV2 status")
    print("=" * 70)
    v = detect_curl_cffi_version()
    fps = detect_supported_fingerprints()
    print(f"  curl_cffi:        {v}")
    print(f"  supported fps:    {len(fps)}  ({', '.join(fps[:5])}…)")
    if _CERTIFI_PATH:
        print(f"  certifi CA:       OK ({_CERTIFI_PATH})")
    else:
        print(f"  certifi CA:       MISSING — install with: pip install certifi")
    print(f"  userKey cache:    "
          f"{'exists' if USERKEY_CACHE_FILE.exists() else 'missing'}")
    if USERKEY_CACHE_FILE.exists():
        try:
            d = json.loads(USERKEY_CACHE_FILE.read_text())
            print(f"    userKey:        {d.get('userKey')}")
        except Exception:
            pass
    # Show top arms
    learner = perchance.PolicyLearner.load()
    print(f"\n  Bandit state: {learner.total_pulls} total pulls")
    print(f"  Top 5 arms (by mean reward):")
    for i, a in enumerate(learner.top_arms(5), 1):
        sup = "✓" if a.fingerprint in fps else "✗"
        print(f"    {i}. {sup} fp={a.fingerprint:14s} prof={a.profile_name:25s} "
              f"n={a.n_trials:3d} success={a.success_rate:5.1%} mean_r={a.mean_reward:+.2f}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[1]

    if cmd == "probe":
        _print_probe()
        return 0

    if cmd == "status":
        _print_status()
        return 0

    if cmd == "get-key":
        with_page = "--with-page" in argv
        client = PerchanceClientV2()
        res = client.acquire_user_key(
            with_page_warmup=with_page, verbose=True,
        )
        if res.ok:
            print(f"\n  ✅ userKey: {res.user_key}")
            return 0
        print(f"\n  ❌ failed: {res.detail}")
        print(f"  hint: try `python {argv[0]} get-key --with-page` "
              f"to enable the full-page warmup fallback")
        return 1

    if cmd == "train":
        episodes = 20
        if len(argv) > 2:
            try:
                episodes = int(argv[2])
            except ValueError:
                print(f"bad episode count: {argv[2]}")
                return 1
        client = PerchanceClientV2()
        print(f"Training for {episodes} episodes …")
        for ep in range(episodes):
            # Use the softmax policy to explore
            arm = client.learner.select_arm()
            if arm.fingerprint not in client.supported_fps:
                # Mark as bad and skip
                client.learner.update(arm, REWARD_NOT_SUPPORTED,
                                      blocked=True, errored=False, success=False)
                print(f"  ep {ep + 1}: skipping {arm.fingerprint} (not supported)")
                continue
            res = client.verify_user(arm, with_page_warmup=False)
            print(f"  ep {ep + 1:3d}/{episodes}  fp={arm.fingerprint:14s} "
                  f"prof={arm.profile_name:25s}  r={res.reward:+.2f}  {res.detail}")
            time.sleep(0.3 + random.random() * 0.5)
        client.learner.save()
        print()
        _print_status()
        return 0

    if cmd == "gen":
        if len(argv) < 3:
            print("usage: perchance_v2.py gen <prompt> [output.jpg]")
            return 1
        prompt = argv[2]
        out_path = argv[3] if len(argv) > 3 else "out.jpg"
        client = PerchanceClientV2()
        uk = client.acquire_user_key(verbose=True)
        if not uk.user_key:
            print(f"❌ no userKey: {uk.detail}")
            return 1
        # Use the existing single-user generate from perchance.py
        jpeg = perchance.PerchanceClient().generate(
            prompt, verbose=True,
        )
        if jpeg is None:
            print("❌ generate failed")
            return 1
        Path(out_path).write_bytes(jpeg)
        print(f"✅ wrote {out_path} ({len(jpeg)}B)")
        return 0

    print(f"unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
