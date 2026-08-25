"""
perchance.py — A curl_cffi-based client for Perchance's free image API.

Goal: Replicate the request flow observed in perchance.org.json HAR and obtain
a `userKey` from image-generation.perchance.org/api/verifyUser.

Architecture:
    ┌──────────────────────────────────────────────────────────┐
    │  PolicyLearner (multi-armed bandit / Q-learning)         │
    │  - State: (fingerprint, header_profile, last_outcome)    │
    │  - Action: pick a request config to try next             │
    │  - Reward: +1 got userKey, 0 nothing, -1 blocked/limited │
    └──────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌──────────────────────────────────────────────────────────┐
    │  PerchanceClient (curl_cffi)                             │
    │  - Loads/saves state from disk                           │
    │  - Replays verifyUser → generate → await → download      │
    │  - Falls back across multiple impersonation profiles     │
    └──────────────────────────────────────────────────────────┘
                              │
                              ▼
                  image-generation.perchance.org

Usage:
    python perchance.py get-key                # acquire & cache a userKey
    python perchance.py gen "a cute boy"        # generate, save JPEG to disk
    python perchance.py train                   # run RL training loop (N episodes)
    python perchance.py status                  # show learned policy + cache
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# We import inside functions so that --help / --status work even if
# curl_cffi is not installed in the user's environment yet.
try:
    from curl_cffi import requests as cffi_requests
    from curl_cffi.requests.exceptions import RequestException
    _CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover
    cffi_requests = None  # type: ignore
    RequestException = Exception  # type: ignore
    _CURL_CFFI_AVAILABLE = False


# =============================================================================
# Configuration
# =============================================================================

# Where we cache learned policies and the userKey between runs.
CACHE_DIR = Path.home() / ".perchance_client"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USERKEY_CACHE_FILE = CACHE_DIR / "userkey.json"
POLICY_STATE_FILE = CACHE_DIR / "policy_state.json"
TRAINING_LOG_FILE = CACHE_DIR / "training_log.jsonl"

# Endpoints reverse-engineered from the HAR.
WRAPPER_PAGE_URL = "https://perchance.org/imageapi?prompt=a%20cute%20booy"
AD_CODE_URL = "https://perchance.org/api/getAccessCodeForAdPoweredStuff"
EMBED_URL = "https://image-generation.perchance.org/embed"
VERIFY_USER_URL = "https://image-generation.perchance.org/api/verifyUser"
GENERATE_URL = "https://image-generation.perchance.org/api/generate"
AWAIT_URL = "https://image-generation.perchance.org/api/awaitExistingGenerationRequest"
QUEUE_POS_URL = "https://image-generation.perchance.org/api/getUserQueuePosition"
DOWNLOAD_VIA_PROXY_URL = "https://image-generation.perchance.org/api/downloadTemporaryImageViaProxy"

# These are the TLS fingerprints curl_cffi knows how to impersonate.
# (We only ship the ones whose BoringSSL handshake Perchance is least likely
#  to instantly reject based on the User-Agent / sec-ch-ua headers we send.)
FINGERPRINTS: list[str] = [
    "chrome146",   # very close to Chrome 152 (Aug 2026)
    "chrome142",
    "chrome136",
    "chrome131",
    "chrome124",
    "chrome119",
    "firefox147",
    "firefox144",
    "safari184",
    "safari180",
    "edge101",
]

# Each "header profile" is a tweak of the headers in the HAR.
# Different profiles may get past different bot-detection heuristics.
HEADER_PROFILES: list[dict[str, str]] = [
    # Profile 0 — the exact HAR snapshot (Chrome 152 / Windows)
    {
        "name": "har-exact-chrome152-win",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
        "accept-language": "en-US,en;q=0.9",
    },
    # Profile 1 — Chrome on macOS (very common real-user profile)
    {
        "name": "chrome-macos",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="146", "Not?A_Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-mobile": "?0",
        "accept-language": "en-US,en;q=0.9",
    },
    # Profile 2 — Firefox on Linux (different fingerprint family)
    {
        "name": "firefox-linux",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
        "sec-ch-ua-platform": None,    # Firefox does not send sec-ch-ua
        "sec-ch-ua": None,
        "sec-ch-ua-mobile": None,
        "accept-language": "en-US,en;q=0.5",
    },
    # Profile 3 — Safari on macOS
    {
        "name": "safari-macos",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.4 Safari/605.1.15"
        ),
        "sec-ch-ua-platform": None,
        "sec-ch-ua": None,
        "sec-ch-ua-mobile": None,
        "accept-language": "en-US,en;q=0.9",
    },
    # Profile 4 — Chrome on Linux (dev box)
    {
        "name": "chrome-linux",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="146", "Not?A_Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-platform": '"Linux"',
        "sec-ch-ua-mobile": "?0",
        "accept-language": "en-US,en;q=0.9",
    },
]


# =============================================================================
# Reward signals — what the agent learns to chase
# =============================================================================

REWARD_GOT_USERKEY = 1.0      # verifyUser returned a real userKey
REWARD_NEUTRAL = 0.0          # call succeeded but no userKey
REWARD_BLOCKED = -1.0         # Cloudflare / Turnstile blocked us
REWARD_NETWORK_ERR = -0.5     # couldn't reach the host at all
REWARD_RATE_LIMITED = -0.2    # 429 / throttled
REWARD_FORBIDDEN = -0.8       # 403 / WAF challenge


# =============================================================================
# Policy Learner (multi-armed bandit over fingerprint × profile combos)
# =============================================================================


@dataclass
class Arm:
    """A single (fingerprint, header_profile) combination."""
    fingerprint: str
    profile_name: str
    n_trials: int = 0
    n_success: int = 0
    n_blocked: int = 0
    n_errored: int = 0
    reward_sum: float = 0.0
    # Optional: track recent rewards for a more stable estimate.
    recent_rewards: list[float] = field(default_factory=list)

    @property
    def mean_reward(self) -> float:
        if self.n_trials == 0:
            return 0.0
        return self.reward_sum / self.n_trials

    @property
    def success_rate(self) -> float:
        return (self.n_success / self.n_trials) if self.n_trials else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "profile_name": self.profile_name,
            "n_trials": self.n_trials,
            "n_success": self.n_success,
            "n_blocked": self.n_blocked,
            "n_errored": self.n_errored,
            "reward_sum": self.reward_sum,
            "recent_rewards": self.recent_rewards[-50:],  # cap memory
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Arm":
        return cls(
            fingerprint=d["fingerprint"],
            profile_name=d["profile_name"],
            n_trials=d.get("n_trials", 0),
            n_success=d.get("n_success", 0),
            n_blocked=d.get("n_blocked", 0),
            n_errored=d.get("n_errored", 0),
            reward_sum=d.get("reward_sum", 0.0),
            recent_rewards=d.get("recent_rewards", []),
        )


class PolicyLearner:
    """
    Multi-armed bandit over (fingerprint, profile) combinations.

    Three policies are supported:
      - "ucb1"  : Upper-Confidence-Bound (theoretical optimum, but never stops exploring)
      - "softmax": Boltzmann exploration (probabilistic, smooth)
      - "epsilon-greedy": Mostly exploit, occasionally explore (simple & effective)

    We default to "softmax" because in practice the agent has a small budget
    of network calls and we want to lock in on the best config fast.

    Why not full Q-learning?
      - The state space here is tiny (one state: "what config to try next").
      - The reward distribution is stationary enough for a bandit.
      - A bandit gives strong guarantees with no hyperparameter tuning.
    """

    def __init__(
        self,
        c: float = 1.4,
        policy: str = "softmax",
        epsilon: float = 0.1,
        temperature: float = 0.5,
    ) -> None:
        self.c = c
        self.policy = policy
        self.epsilon = epsilon
        self.temperature = temperature
        self.arms: list[Arm] = []
        self.total_pulls: int = 0
        for fp in FINGERPRINTS:
            for prof in HEADER_PROFILES:
                self.arms.append(
                    Arm(fingerprint=fp, profile_name=prof["name"])
                )

    def select_arm(self) -> Arm:
        # Always try an untried arm first
        untried = [a for a in self.arms if a.n_trials == 0]
        if untried:
            return random.choice(untried)

        if self.policy == "ucb1":
            return self._select_ucb1()
        elif self.policy == "epsilon-greedy":
            return self._select_epsilon_greedy()
        else:
            return self._select_softmax()

    def _select_ucb1(self) -> Arm:
        log_n = math.log(max(self.total_pulls, 1))
        scores: list[float] = []
        for a in self.arms:
            exploit = a.mean_reward
            explore = self.c * math.sqrt(log_n / a.n_trials)
            scores.append(exploit + explore)
        max_score = max(scores)
        candidates = [i for i, s in enumerate(scores) if s == max_score]
        return self.arms[random.choice(candidates)]

    def _select_epsilon_greedy(self) -> Arm:
        if random.random() < self.epsilon:
            return random.choice(self.arms)
        # Exploit: pick the arm with the highest mean reward
        best_arm = max(self.arms, key=lambda a: a.mean_reward)
        return best_arm

    def _select_softmax(self) -> Arm:
        # Boltzmann / softmax over mean rewards
        rewards = [a.mean_reward for a in self.arms]
        max_r = max(rewards)
        exps = [math.exp((r - max_r) / max(self.temperature, 1e-6)) for r in rewards]
        total = sum(exps)
        probs = [e / total for e in exps]
        # Sample
        r = random.random()
        cum = 0.0
        for a, p in zip(self.arms, probs):
            cum += p
            if r <= cum:
                return a
        return self.arms[-1]

    def update(self, arm: Arm, reward: float, *, blocked: bool, errored: bool, success: bool) -> None:
        arm.n_trials += 1
        arm.reward_sum += reward
        arm.recent_rewards.append(reward)
        if len(arm.recent_rewards) > 50:
            arm.recent_rewards.pop(0)
        if success:
            arm.n_success += 1
        if blocked:
            arm.n_blocked += 1
        if errored:
            arm.n_errored += 1
        self.total_pulls += 1

    # ---------- persistence ----------

    def save(self) -> None:
        data = {
            "total_pulls": self.total_pulls,
            "c": self.c,
            "policy": self.policy,
            "epsilon": self.epsilon,
            "temperature": self.temperature,
            "arms": [a.to_dict() for a in self.arms],
        }
        POLICY_STATE_FILE.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> "PolicyLearner":
        if not POLICY_STATE_FILE.exists():
            return cls()
        try:
            data = json.loads(POLICY_STATE_FILE.read_text())
            learner = cls(
                c=data.get("c", 1.4),
                policy=data.get("policy", "softmax"),
                epsilon=data.get("epsilon", 0.1),
                temperature=data.get("temperature", 0.5),
            )
            learner.total_pulls = data.get("total_pulls", 0)
            # Merge persisted arms with current set (in case code changed)
            persisted_by_key = {
                (a["fingerprint"], a["profile_name"]): a for a in data.get("arms", [])
            }
            new_arms: list[Arm] = []
            for arm in learner.arms:
                key = (arm.fingerprint, arm.profile_name)
                if key in persisted_by_key:
                    new_arms.append(Arm.from_dict(persisted_by_key[key]))
                else:
                    new_arms.append(arm)
            learner.arms = new_arms
            return learner
        except Exception:
            return cls()

    def top_arms(self, k: int = 5) -> list[Arm]:
        return sorted(
            self.arms, key=lambda a: a.mean_reward, reverse=True
        )[:k]


# =============================================================================
# Perchance client
# =============================================================================


class PerchanceError(Exception):
    """Base error for Perchance client."""


class CloudflareBlocked(PerchanceError):
    """Cloudflare or Turnstile blocked the request."""


class RateLimited(PerchanceError):
    """Server returned 429 or throttled response."""


class UserKeyNotObtained(PerchanceError):
    """verifyUser did not return a userKey."""


@dataclass
class PerchanceResult:
    ok: bool
    user_key: Optional[str] = None
    ad_access_code: Optional[str] = None
    reward: float = 0.0
    blocked: bool = False
    errored: bool = False
    success: bool = False
    detail: str = ""


class PerchanceClient:
    """
    A non-browser client for Perchance's image API.

    Flow (mirrors the HAR exactly):
        1. GET /api/getAccessCodeForAdPoweredStuff          (adAccessCode)
        2. GET /api/verifyUser?thread=0                     (userKey)
        3. (optional) POST /api/generate  … (image gen)
    """

    def __init__(
        self,
        learner: Optional[PolicyLearner] = None,
        *,
        proxy: Optional[str] = None,
        timeout: float = 20.0,
    ) -> None:
        self.learner = learner or PolicyLearner.load()
        self.proxy = proxy
        self.timeout = timeout
        self._session = None

    # ---------- session management ----------

    def _make_session(self, arm: Arm):
        """Build a curl_cffi session with the given arm's config."""
        if not _CURL_CFFI_AVAILABLE:
            raise PerchanceError(
                "curl_cffi is not installed. Run: "
                "pip install --break-system-packages curl_cffi"
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
        if self.proxy:
            sess.proxies = {"http": self.proxy, "https": self.proxy}
        return sess

    def _classify_failure(self, response) -> tuple[float, bool, bool, bool, str]:
        """Map an HTTP response to (reward, blocked, errored, success, detail)."""
        sc = response.status_code
        body = response.text or ""
        if sc == 200:
            return 0.0, False, False, True, f"200 OK ({len(body)}B)"
        if sc in (403, 407):
            return REWARD_FORBIDDEN, True, False, False, f"{sc} forbidden/waf"
        if sc in (429, 503):
            return REWARD_RATE_LIMITED, True, False, False, f"{sc} throttled"
        if sc in (520, 521, 522, 523, 524, 525, 526, 527, 530):
            return REWARD_BLOCKED, True, False, False, f"{sc} cloudflare error"
        if sc >= 500:
            return REWARD_NETWORK_ERR, False, True, False, f"{sc} server error"
        return REWARD_BLOCKED, True, False, False, f"unexpected {sc}"

    # ---------- public API ----------

    def acquire_user_key(
        self,
        *,
        arm: Optional[Arm] = None,
        verbose: bool = True,
    ) -> PerchanceResult:
        """
        Step 1: hit /api/verifyUser and extract a userKey.
        """
        if not _CURL_CFFI_AVAILABLE:
            return PerchanceResult(
                ok=False, errored=True, detail="curl_cffi not installed",
                reward=REWARD_NETWORK_ERR,
            )

        arm = arm or self.learner.select_arm()
        if verbose:
            print(f"  [arm] fp={arm.fingerprint}  profile={arm.profile_name}")

        sess = self._make_session(arm)
        url = f"{VERIFY_USER_URL}?thread=0&__cacheBust={random.random()}"

        try:
            r = sess.get(url, timeout=self.timeout)
        except RequestException as e:
            self.learner.update(
                arm, REWARD_NETWORK_ERR,
                blocked=False, errored=True, success=False,
            )
            return PerchanceResult(
                ok=False, errored=True,
                detail=f"network error: {str(e)[:200]}",
                reward=REWARD_NETWORK_ERR,
            )
        except Exception as e:
            self.learner.update(
                arm, REWARD_NETWORK_ERR,
                blocked=False, errored=True, success=False,
            )
            return PerchanceResult(
                ok=False, errored=True,
                detail=f"unexpected: {type(e).__name__}: {str(e)[:200]}",
                reward=REWARD_NETWORK_ERR,
            )

        reward, blocked, errored, success, detail = self._classify_failure(r)

        # Did the body contain a userKey?
        user_key: Optional[str] = None
        if success:
            try:
                j = r.json()
                user_key = j.get("userKey")
            except Exception:
                # Body might be 106 B plain text — try regex
                m = re.search(r'"userKey"\s*:\s*"([a-f0-9]{64})"', r.text)
                if m:
                    user_key = m.group(1)
            if user_key:
                reward = REWARD_GOT_USERKEY
                success = True
                detail = f"GOT userKey (status {j.get('status', '?') if isinstance(j, dict) else 'regex'})"
            else:
                # 200 OK but no userKey — keep neutral reward
                detail = f"200 OK but no userKey in body: {r.text[:200]}"

        self.learner.update(
            arm, reward, blocked=blocked, errored=errored,
            success=(user_key is not None),
        )
        self.learner.save()

        return PerchanceResult(
            ok=user_key is not None,
            user_key=user_key,
            reward=reward,
            blocked=blocked,
            errored=errored,
            success=(user_key is not None),
            detail=detail,
        )

    def acquire_ad_access_code(
        self,
        *,
        verbose: bool = True,
    ) -> Optional[str]:
        """
        Step 0: fetch adAccessCode from perchance.org.
        NOTE: This is the endpoint most likely to be Cloudflare-gated.
        If blocked, we still have a shot at verifyUser (which lives on a
        different origin and may have a different WAF policy).
        """
        if not _CURL_CFFI_AVAILABLE:
            return None
        arm = self.learner.select_arm()
        sess = self._make_session(arm)
        url = f"{AD_CODE_URL}?__cacheBust={random.random()}"
        try:
            r = sess.get(url, timeout=self.timeout)
        except Exception as e:
            if verbose:
                print(f"  [ad-code] network error: {str(e)[:120]}")
            return None
        if r.status_code != 200:
            if verbose:
                print(f"  [ad-code] non-200: {r.status_code} {r.text[:120]}")
            return None
        body = r.text.strip().strip('"')
        if re.fullmatch(r"[a-f0-9]{64}", body):
            return body
        if verbose:
            print(f"  [ad-code] unexpected body: {body[:120]}")
        return None

    def get_user_key(
        self,
        *,
        max_attempts: int = 5,
        verbose: bool = True,
    ) -> PerchanceResult:
        """
        Try to get a userKey with the best learned config.
        Falls back to other arms if the chosen one fails.
        """
        # Check cache first
        cached = self._load_cached_user_key()
        if cached:
            if verbose:
                print(f"  [cache] using cached userKey")
            return PerchanceResult(
                ok=True, user_key=cached, success=True,
                detail="loaded from cache", reward=REWARD_GOT_USERKEY,
            )

        if verbose:
            print("  [learner] selecting arm via UCB1 …")
        last_err: Optional[PerchanceResult] = None
        attempts_made = 0
        tried_arms: set[int] = set()

        for attempt in range(max_attempts):
            arm = self.learner.select_arm()
            # Force exploration: try a different arm each attempt until
            # we have at least one good result.
            while id(arm) in tried_arms and attempt < len(self.learner.arms) - 1:
                arm = self.learner.arms[(attempt + 1) % len(self.learner.arms)]
            tried_arms.add(id(arm))

            if verbose:
                print(f"  [attempt {attempt + 1}/{max_attempts}] fp={arm.fingerprint} prof={arm.profile_name}")
            res = self.acquire_user_key(arm=arm, verbose=False)
            attempts_made += 1
            last_err = res

            if res.ok and res.user_key:
                self._save_user_key(res.user_key)
                if verbose:
                    print(f"  [ok] userKey: {res.user_key}")
                    print(f"  [ok] {res.detail}")
                return res

            if verbose:
                print(f"  [fail] {res.detail}")

            # small backoff
            time.sleep(0.5 + random.random())

        # If we couldn't get one, return the last failure
        if last_err is None:
            last_err = PerchanceResult(
                ok=False, detail="no attempts made",
                errored=True, reward=REWARD_NETWORK_ERR,
            )
        return last_err

    def generate(
        self,
        prompt: str,
        *,
        resolution: str = "512x768",
        guidance_scale: float = 7.0,
        seed: int = -1,
        negative_prompt: str = "",
        timeout: float = 180.0,
        verbose: bool = True,
    ) -> Optional[bytes]:
        """
        Submit a generation, poll the queue, and download the JPEG.
        Returns raw JPEG bytes on success, None on failure.
        """
        if not _CURL_CFFI_AVAILABLE:
            raise PerchanceError("curl_cffi not installed")

        # 1) ensure we have a userKey
        if verbose:
            print("  [gen] ensuring userKey …")
        uk_res = self.get_user_key(verbose=verbose)
        if not uk_res.user_key:
            raise UserKeyNotObtained(f"no userKey: {uk_res.detail}")
        user_key = uk_res.user_key

        # 2) ensure we have an adAccessCode
        if verbose:
            print("  [gen] ensuring adAccessCode …")
        ad_code = self.acquire_ad_access_code(verbose=verbose)
        if not ad_code:
            if verbose:
                print("  [gen] no adAccessCode — will still try (some endpoints accept without)")

        # 3) submit generate
        arm = self.learner.select_arm()
        sess = self._make_session(arm)
        request_id = str(random.random())
        qs = {
            "userKey": user_key,
            "requestId": request_id,
            "adAccessCode": ad_code or "",
            "__cacheBust": str(random.random()),
        }
        # remove empty adAccessCode
        if not qs["adAccessCode"]:
            del qs["adAccessCode"]
        url = GENERATE_URL + "?" + "&".join(f"{k}={v}" for k, v in qs.items())

        body = json.dumps({
            "prompt": prompt,
            "negativePrompt": negative_prompt,
            "seed": seed,
            "resolution": resolution,
            "guidanceScale": guidance_scale,
            "channel": "imageapi",
            "subChannel": "public",
            "userKey": user_key,
            "adAccessCode": ad_code or "",
            "requestId": request_id,
        })
        # if no ad_code, strip it from body too
        if not ad_code:
            body = body.replace(', "adAccessCode":""', "").replace('"adAccessCode":"", ', "").replace('"adAccessCode":""', "")

        if verbose:
            print(f"  [gen] POST /api/generate  prompt={prompt!r}  resolution={resolution}")

        try:
            r = sess.post(
                url,
                data=body,
                headers={
                    "content-type": "text/plain;charset=UTF-8",
                    "origin": "https://image-generation.perchance.org",
                    "referer": "https://image-generation.perchance.org/embed",
                },
                timeout=self.timeout,
            )
        except Exception as e:
            if verbose:
                print(f"  [gen] network error: {e}")
            return None

        if verbose:
            print(f"  [gen] response: {r.status_code} {len(r.text)}B  body={r.text[:200]}")

        if r.status_code != 200:
            return None
        try:
            j = r.json()
        except Exception:
            j = {}

        # 4) poll
        if j.get("status") in ("rate_limited", "already_in_queue") or len(r.text) < 100:
            if verbose:
                print("  [gen] queued / throttled — polling …")
            for poll in range(60):  # up to 60s
                time.sleep(1.0)
                try:
                    rp = sess.get(
                        f"{AWAIT_URL}?userKey={user_key}&__cacheBust={random.random()}",
                        timeout=self.timeout,
                    )
                    rq = sess.get(
                        f"{QUEUE_POS_URL}?userKey={user_key}&requestId={request_id}&__cacheBust={random.random()}",
                        timeout=self.timeout,
                    )
                except Exception:
                    continue
                if verbose and (poll % 5 == 0):
                    print(f"  [gen] poll #{poll}: await={rp.text[:60]!r}  queue={rq.text[:60]!r}")
                # Heuristic: queue 79B / 25B responses indicate the image is ready
                if rq.status_code == 200 and ("ready" in rq.text.lower() or rq.text.strip() in ("0", '{"queuePosition":0}')):
                    break
        # The HAR showed a small response (605–620 B) on success containing imageUrl/t token.
        # Try to extract any imageUrl from the generate response.
        image_url: Optional[str] = None
        t_token: Optional[str] = None
        try:
            j = r.json()
            image_url = j.get("imageUrl")
            t_token = j.get("t") or j.get("imageToken")
            # Some implementations nest it
            if not image_url and isinstance(j.get("result"), dict):
                image_url = j["result"].get("url")
                t_token = t_token or j["result"].get("t")
            # Or the whole body might be a list of objects
            if not image_url and isinstance(j, list) and j:
                first = j[0]
                if isinstance(first, dict):
                    image_url = first.get("url")
                    t_token = t_token or first.get("t")
        except Exception:
            pass

        # If we don't have an imageUrl from the generate response, try polling once more
        if not image_url and not t_token:
            if verbose:
                print("  [gen] no imageUrl in initial response, doing one more poll …")
            for _ in range(30):
                time.sleep(2.0)
                try:
                    rp = sess.get(
                        f"{AWAIT_URL}?userKey={user_key}&__cacheBust={random.random()}",
                        timeout=self.timeout,
                    )
                except Exception:
                    continue
                try:
                    jj = rp.json()
                    image_url = jj.get("imageUrl")
                    t_token = jj.get("t") or jj.get("imageToken")
                    if not image_url and isinstance(jj.get("result"), dict):
                        image_url = jj["result"].get("url")
                        t_token = t_token or jj["result"].get("t")
                    if image_url or t_token:
                        break
                except Exception:
                    continue

        if not image_url and not t_token:
            if verbose:
                print("  [gen] FAILED to get imageUrl/t — server may have refused.")
            return None

        # 5) download
        if t_token and not image_url:
            image_url = f"{DOWNLOAD_VIA_PROXY_URL}?t={t_token}"
        if verbose:
            print(f"  [gen] GET {image_url[:120]}")
        try:
            rd = sess.get(image_url, timeout=self.timeout)
        except Exception as e:
            if verbose:
                print(f"  [gen] download network error: {e}")
            return None
        if rd.status_code != 200 or "image" not in rd.headers.get("content-type", ""):
            if verbose:
                print(f"  [gen] download failed: {rd.status_code} ct={rd.headers.get('content-type')}")
            return None
        if verbose:
            print(f"  [gen] downloaded JPEG: {len(rd.content)}B")
        return rd.content

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
            "userKey": user_key,
            "savedAt": time.time(),
        }, indent=2))


# =============================================================================
# Training loop
# =============================================================================


def run_training(episodes: int = 20, verbose: bool = True) -> None:
    """
    Multi-armed bandit training: try many (fingerprint, profile) combinations
    and let the agent learn which ones are least likely to be blocked.
    """
    if not _CURL_CFFI_AVAILABLE:
        print("ERROR: curl_cffi not installed. Aborting.")
        sys.exit(1)

    learner = PolicyLearner.load()
    client = PerchanceClient(learner=learner)

    print(f"Training for {episodes} episodes …")
    print(f"Arms in play: {len(learner.arms)}")
    print()

    log_lines: list[str] = []
    for ep in range(episodes):
        arm = learner.select_arm()
        if verbose:
            print(f"--- ep {ep + 1}/{episodes}  fp={arm.fingerprint}  prof={arm.profile_name} ---")
        res = client.acquire_user_key(arm=arm, verbose=False)
        if verbose:
            print(f"  reward={res.reward:+.2f}  detail={res.detail}")

        log_lines.append(json.dumps({
            "episode": ep,
            "arm": arm.to_dict(),
            "reward": res.reward,
            "ok": res.ok,
            "user_key": res.user_key,
            "detail": res.detail,
        }))
        # Persist after every episode
        TRAINING_LOG_FILE.write_text("\n".join(log_lines) + "\n")
        learner.save()

        # short sleep so we don't DOS the target
        time.sleep(0.3 + random.random() * 0.5)

    print()
    print("=== Top 5 arms by mean reward ===")
    for i, a in enumerate(learner.top_arms(5), 1):
        print(f"  {i}. fp={a.fingerprint:20s}  prof={a.profile_name:25s}  "
              f"n={a.n_trials:3d}  success={a.success_rate:.1%}  "
              f"mean_r={a.mean_reward:+.2f}")


# =============================================================================
# CLI
# =============================================================================


def _print_status() -> None:
    print("=== PerchanceClient status ===")
    print(f"Cache dir:           {CACHE_DIR}")
    print(f"userKey cache file:  {USERKEY_CACHE_FILE}  "
          f"({'exists' if USERKEY_CACHE_FILE.exists() else 'missing'})")
    if USERKEY_CACHE_FILE.exists():
        try:
            d = json.loads(USERKEY_CACHE_FILE.read_text())
            print(f"  cached userKey:    {d.get('userKey')}")
        except Exception:
            pass
    print(f"Policy state file:   {POLICY_STATE_FILE}  "
          f"({'exists' if POLICY_STATE_FILE.exists() else 'missing'})")
    if POLICY_STATE_FILE.exists():
        learner = PolicyLearner.load()
        print(f"  total pulls:       {learner.total_pulls}")
        print(f"  arms tracked:      {len(learner.arms)}")
        print(f"  top 5 arms:")
        for i, a in enumerate(learner.top_arms(5), 1):
            print(f"    {i}. fp={a.fingerprint:20s} prof={a.profile_name:25s} "
                  f"n={a.n_trials:3d} success={a.success_rate:.1%} mean_r={a.mean_reward:+.2f}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 0

    cmd = argv[1]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    if cmd == "status":
        _print_status()
        return 0

    if cmd == "get-key":
        client = PerchanceClient()
        res = client.get_user_key(verbose=True)
        if res.ok:
            print(f"\n  ✅ userKey: {res.user_key}")
            return 0
        print(f"\n  ❌ failed: {res.detail}")
        return 1

    if cmd == "train":
        episodes = 20
        if len(argv) > 2:
            try:
                episodes = int(argv[2])
            except ValueError:
                print(f"bad episode count: {argv[2]}")
                return 1
        run_training(episodes=episodes)
        return 0

    if cmd == "gen":
        if len(argv) < 3:
            print("usage: perchance.py gen <prompt> [output.jpg]")
            return 1
        prompt = argv[2]
        out_path = argv[3] if len(argv) > 3 else "out.jpg"
        client = PerchanceClient()
        jpeg = client.generate(prompt, verbose=True)
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
