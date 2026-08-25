"""
multi_proxy.py — A production-grade multi-user userKey acquisition system.

Architecture:

    ┌──────────────────────────────────────────────────────────────┐
    │  ProxyMinerClient                                            │
    │  - Reads proxy-miner's SQLite DB                             │
    │  - Exports 50 (or N) working proxies                         │
    │  - Filters by protocol, country, anonymity                   │
    └──────────────────────────────────────────────────────────────┘
                              │ proxies.txt
                              ▼
    ┌──────────────────────────────────────────────────────────────┐
    │  ProxyHealthChecker                                          │
    │  - Pings each proxy through a known target                   │
    │  - Keeps only the ones that actually work                    │
    │  - Measures latency + anonymity                              │
    └──────────────────────────────────────────────────────────────┘
                              │ healthy_proxies.json
                              ▼
    ┌──────────────────────────────────────────────────────────────┐
    │  UserKeyFarm                                                │
    │  - Spawns N "users" (one per proxy)                          │
    │  - Each user runs a PerchanceClient through its proxy        │
    │  - Each user has its own (fingerprint, header profile)       │
    │  - A central BanditManager learns which (proxy,fp,profile)   │
    │    combos succeed, and re-assigns users to better arms       │
    │  - Saves each userKey to a separate file                     │
    └──────────────────────────────────────────────────────────────┘
                              │
                              ▼
                  image-generation.perchance.org

Usage:
    # 1. Extract 50 proxies from the miner DB
    python3 multi_proxy.py extract --count 50 --out proxies.txt

    # 2. Health-check them (drops dead ones)
    python3 multi_proxy.py healthcheck --in proxies.txt --out live_proxies.json

    # 3. Farm 20 userKeys (one per user, one per proxy)
    python3 multi_proxy.py farm --users 20 --out userkeys/

    # 4. Show what we got
    python3 multi_proxy.py status
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Optional

# Reuse the bandit + reward classification from perchance.py
sys.path.insert(0, str(Path(__file__).parent))
import perchance  # noqa: E402

# curl_cffi is optional for extract/healthcheck; required for farm
try:
    from curl_cffi import requests as cffi_requests
    from curl_cffi.requests.exceptions import RequestException
    _CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover
    cffi_requests = None
    RequestException = Exception
    _CURL_CFFI_AVAILABLE = False


CACHE_DIR = Path.home() / ".perchance_client"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

PROXIES_TXT_FILE = CACHE_DIR / "proxies.txt"
LIVE_PROXIES_FILE = CACHE_DIR / "live_proxies.json"
USERKEYS_DIR = CACHE_DIR / "userkeys"
USERKEYS_DIR.mkdir(parents=True, exist_ok=True)
FARM_LOG_FILE = CACHE_DIR / "farm_log.jsonl"
FARM_STATE_FILE = CACHE_DIR / "farm_state.json"

# Where the proxy-miner DB lives (relative to this script's sibling repo)
PROXY_MINER_DB_CANDIDATES = [
    Path.home() / "RepopoxRev" / "proxy-miner" / "data" / "proxies.db",
    Path("/tmp/RepopoxRev/proxy-miner/data/proxies.db"),
    Path(__file__).parent / "RepopoxRev" / "proxy-miner" / "data" / "proxies.db",
    Path(__file__).parent.parent / "RepopoxRev" / "proxy-miner" / "data" / "proxies.db",
    Path(__file__).parent / "proxy-miner" / "data" / "proxies.db",
]


# =============================================================================
# Proxy extraction (reads the proxy-miner SQLite DB)
# =============================================================================


def find_proxy_miner_db() -> Optional[Path]:
    """Find the proxy-miner SQLite database by checking known locations."""
    for p in PROXY_MINER_DB_CANDIDATES:
        if p.exists():
            return p
    return None


@dataclass
class Proxy:
    """A single proxy record."""
    ip: str
    port: int
    proxy: str  # "ip:port"
    country: Optional[str] = None
    country_code: Optional[str] = None
    protocols: list[str] = field(default_factory=list)
    anonymity: Optional[str] = None
    risk_level: Optional[str] = None
    source: Optional[str] = None
    last_seen: Optional[str] = None

    def url(self, scheme: str = "http") -> str:
        return f"{scheme}://{self.ip}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_proxies(
    db_path: Path,
    *,
    count: int = 50,
    protocol: Optional[str] = None,
    country_code: Optional[str] = None,
    anonymity: Optional[str] = None,
    seed: Optional[int] = None,
) -> list[Proxy]:
    """
    Extract proxies from the miner DB.

    Filtering (all optional):
      - protocol: 'HTTP', 'HTTPS', 'SOCKS4', 'SOCKS5'
      - country_code: 'US', 'DE', 'IN', etc.
      - anonymity: 'Elite', 'Anonymous', 'Transparent'

    Selection strategy (when count < total):
      - Diversify across countries and protocols first
      - Then fill by random selection (with the seed for reproducibility)
    """
    rng = random.Random(seed) if seed is not None else random
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Build query
    where: list[str] = []
    args: list[Any] = []
    if protocol:
        where.append("EXISTS (SELECT 1 FROM proxy_protocols pp "
                     "WHERE pp.proxy = p.proxy AND pp.protocol = ?)")
        args.append(protocol.upper())
    if country_code:
        where.append("p.country_code = ?")
        args.append(country_code.upper())
    if anonymity:
        where.append("p.anonymity = ?")
        args.append(anonymity)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT p.proxy, p.ip, p.port, p.country, p.country_code, p.anonymity,
               p.risk_level, p.source, p.last_seen,
               (SELECT group_concat(protocol) FROM proxy_protocols pp
                WHERE pp.proxy = p.proxy) AS protocols
        FROM proxies p
        {clause}
        ORDER BY p.last_updated DESC
    """
    rows = conn.execute(sql, args).fetchall()
    conn.close()

    proxies = []
    for r in rows:
        protos = [p for p in (r["protocols"] or "").split(",") if p]
        if not protos:
            continue
        # If the user asked for a specific protocol, only keep that one
        if protocol and protocol.upper() not in protos:
            continue
        p = Proxy(
            ip=r["ip"],
            port=r["port"],
            proxy=r["proxy"],
            country=r["country"],
            country_code=r["country_code"],
            protocols=protos,
            anonymity=r["anonymity"],
            risk_level=r["risk_level"],
            source=r["source"],
            last_seen=r["last_seen"],
        )
        proxies.append(p)

    if not proxies:
        return []

    # Diversify: bucket by (country_code, primary_protocol), then sample
    buckets: dict[tuple, list[Proxy]] = {}
    for p in proxies:
        primary = p.protocols[0]
        key = (p.country_code or "XX", primary)
        buckets.setdefault(key, []).append(p)

    # Round-robin across buckets
    selected: list[Proxy] = []
    bucket_keys = list(buckets.keys())
    rng.shuffle(bucket_keys)
    while len(selected) < count and any(buckets[k] for k in bucket_keys):
        for k in bucket_keys:
            if len(selected) >= count:
                break
            if buckets[k]:
                selected.append(buckets[k].pop(0))

    return selected


def write_proxies_txt(proxies: list[Proxy], path: Path) -> None:
    """Write proxies in the standard `ip:port` one-per-line format."""
    lines = [p.proxy for p in proxies]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    print(f"  wrote {len(lines)} proxies → {path}")


# =============================================================================
# Health checking
# =============================================================================


@dataclass
class HealthResult:
    proxy: str
    ok: bool
    latency_ms: Optional[float] = None
    via: Optional[str] = None  # which protocol worked
    error: Optional[str] = None
    checked_at: float = field(default_factory=time.time)


def _check_one(proxy: Proxy, target_url: str, timeout: float) -> HealthResult:
    """Try the proxy against a target. Returns the first working protocol."""
    if not _CURL_CFFI_AVAILABLE:
        return HealthResult(
            proxy=proxy.proxy, ok=False, error="curl_cffi not installed",
        )
    for proto in proxy.protocols:
        scheme = proto.lower()
        url = proxy.url(scheme)
        t0 = time.monotonic()
        try:
            r = cffi_requests.get(
                target_url,
                proxies={scheme: url},
                impersonate="chrome131",
                timeout=timeout,
                allow_redirects=False,
            )
            latency = (time.monotonic() - t0) * 1000
            if 200 <= r.status_code < 400:
                return HealthResult(
                    proxy=proxy.proxy, ok=True, latency_ms=latency, via=proto,
                )
            return HealthResult(
                proxy=proxy.proxy, ok=False, latency_ms=latency,
                via=proto, error=f"HTTP {r.status_code}",
            )
        except RequestException as e:
            return HealthResult(
                proxy=proxy.proxy, ok=False, via=proto,
                error=f"{type(e).__name__}: {str(e)[:120]}",
            )
        except Exception as e:
            return HealthResult(
                proxy=proxy.proxy, ok=False, via=proto,
                error=f"{type(e).__name__}: {str(e)[:120]}",
            )
    return HealthResult(proxy=proxy.proxy, ok=False, error="no working protocol")


def healthcheck_proxies(
    proxies: list[Proxy],
    *,
    target_url: str = "https://www.cloudflare.com/cdn-cgi/trace",
    timeout: float = 8.0,
    max_workers: int = 20,
) -> list[HealthResult]:
    """
    Health-check all proxies in parallel.
    Cloudflare's /cdn-cgi/trace is a good target because:
      - It returns 200 OK to almost anything
      - It tells you the source IP (so we can confirm anonymity)
      - It's a small response (~200 B)
    """
    if not _CURL_CFFI_AVAILABLE:
        print("  ERROR: curl_cffi not installed — skipping healthcheck")
        return [HealthResult(proxy=p.proxy, ok=False, error="curl_cffi missing")
                for p in proxies]
    results: list[HealthResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_check_one, p, target_url, timeout): p for p in proxies}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            results.append(res)
            done += 1
            if done % 10 == 0 or done == len(proxies):
                ok = sum(1 for r in results if r.ok)
                print(f"  healthcheck progress: {done}/{len(proxies)} ({ok} alive)")
    return results


def write_live_proxies(
    proxies: list[Proxy],
    results: list[HealthResult],
    path: Path,
) -> list[Proxy]:
    """Filter proxies to only the live ones and persist them."""
    proxy_by_addr = {p.proxy: p for p in proxies}
    live: list[Proxy] = []
    for r in results:
        if r.ok and r.proxy in proxy_by_addr:
            p = proxy_by_addr[r.proxy]
            # Mark the protocol that worked as preferred
            if r.via:
                p.protocols = [r.via] + [x for x in p.protocols if x != r.via]
            live.append(p)
    payload = {
        "checked_at": time.time(),
        "target": "https://www.cloudflare.com/cdn-cgi/trace",
        "live_count": len(live),
        "dead_count": len(proxies) - len(live),
        "proxies": [p.to_dict() for p in live],
    }
    path.write_text(json.dumps(payload, indent=2))
    return live


# =============================================================================
# UserKey farming
# =============================================================================


@dataclass
class UserSlot:
    """One 'user' = one proxy + one (fingerprint, profile) config."""
    user_id: int
    proxy: Optional[Proxy] = None
    arm: Optional[perchance.Arm] = None
    user_key: Optional[str] = None
    attempts: int = 0
    last_reward: float = 0.0
    last_detail: str = ""
    last_attempt_at: float = 0.0

    def save(self) -> None:
        path = USERKEYS_DIR / f"user_{self.user_id:02d}.json"
        path.write_text(json.dumps({
            "user_id": self.user_id,
            "proxy": self.proxy.to_dict() if self.proxy else None,
            "arm": self.arm.to_dict() if self.arm else None,
            "user_key": self.user_key,
            "attempts": self.attempts,
            "last_reward": self.last_reward,
            "last_detail": self.last_detail,
            "last_attempt_at": self.last_attempt_at,
        }, indent=2))


class UserKeyFarm:
    """
    Manages N users, each with its own proxy + arm, all sharing a central
    bandit learner. The bandit picks arms; we assign them to users.
    """

    def __init__(
        self,
        live_proxies: list[Proxy],
        *,
        num_users: int = 20,
        bandit: Optional[perchance.PolicyLearner] = None,
        client_timeout: float = 20.0,
    ) -> None:
        if num_users > len(live_proxies):
            print(f"  WARN: asked for {num_users} users but only "
                  f"{len(live_proxies)} live proxies — using {len(live_proxies)}")
            num_users = len(live_proxies)
        self.num_users = num_users
        self.live_proxies = live_proxies
        self.bandit = bandit or perchance.PolicyLearner.load()
        self.client_timeout = client_timeout
        self.users: list[UserSlot] = []
        self._init_users()

    def _init_users(self) -> None:
        """Assign one proxy per user; bandit will pick the arm for each."""
        random.shuffle(self.live_proxies)
        for i in range(self.num_users):
            self.users.append(UserSlot(
                user_id=i + 1,
                proxy=self.live_proxies[i],
                arm=self.bandit.select_arm(),
            ))

    def _make_session_for(self, user: UserSlot):
        """Create a curl_cffi Session bound to a specific user's proxy + arm."""
        if not _CURL_CFFI_AVAILABLE:
            raise RuntimeError("curl_cffi not installed")
        profile = next(
            p for p in perchance.HEADER_PROFILES
            if p["name"] == user.arm.profile_name
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

        sess = cffi_requests.Session(impersonate=user.arm.fingerprint)
        sess.headers.update(headers)
        # Bind the session to this user's proxy.
        if user.proxy:
            # Prefer the protocol that worked in healthcheck (now protocols[0])
            proto = user.proxy.protocols[0].lower() if user.proxy.protocols else "http"
            sess.proxies = {proto: user.proxy.url(proto)}
        return sess

    def _attempt_user(self, user: UserSlot) -> perchance.PerchanceResult:
        """Make a single userKey acquisition attempt for one user."""
        if user.arm is None:
            user.arm = self.bandit.select_arm()
        try:
            sess = self._make_session_for(user)
        except Exception as e:
            return perchance.PerchanceResult(
                ok=False, errored=True,
                detail=f"session-build: {e}",
                reward=perchance.REWARD_NETWORK_ERR,
            )
        url = (f"{perchance.VERIFY_USER_URL}"
               f"?thread={user.user_id}&__cacheBust={random.random()}")
        try:
            r = sess.get(url, timeout=self.client_timeout)
        except RequestException as e:
            return perchance.PerchanceResult(
                ok=False, errored=True,
                detail=f"network: {type(e).__name__}: {str(e)[:120]}",
                reward=perchance.REWARD_NETWORK_ERR,
            )
        except Exception as e:
            return perchance.PerchanceResult(
                ok=False, errored=True,
                detail=f"unexpected: {type(e).__name__}: {str(e)[:120]}",
                reward=perchance.REWARD_NETWORK_ERR,
            )
        reward, blocked, errored, success, detail = self._classify(r)
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
                detail = f"GOT userKey (status verified)"
        return perchance.PerchanceResult(
            ok=user_key is not None,
            user_key=user_key,
            reward=reward,
            blocked=blocked,
            errored=errored,
            success=(user_key is not None),
            detail=detail,
        )

    @staticmethod
    def _classify(response) -> tuple[float, bool, bool, bool, str]:
        sc = response.status_code
        if sc == 200:
            return 0.0, False, False, True, f"200 OK ({len(response.text)}B)"
        if sc in (403, 407):
            return perchance.REWARD_FORBIDDEN, True, False, False, f"{sc} forbidden/waf"
        if sc in (429, 503):
            return perchance.REWARD_RATE_LIMITED, True, False, False, f"{sc} throttled"
        if sc in (520, 521, 522, 523, 524, 525, 526, 527, 530):
            return perchance.REWARD_BLOCKED, True, False, False, f"{sc} cloudflare error"
        if sc >= 500:
            return perchance.REWARD_NETWORK_ERR, False, True, False, f"{sc} server error"
        return perchance.REWARD_BLOCKED, True, False, False, f"unexpected {sc}"

    def run(
        self,
        *,
        max_attempts_per_user: int = 5,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """
        Try to acquire a userKey for every user. Returns a summary dict.
        """
        acquired = 0
        total_attempts = 0
        log_lines: list[str] = []
        t_start = time.monotonic()

        # Per-user loop. For each user, we try a few times, swapping arms
        # if the current one is failing.
        for user in self.users:
            if verbose:
                print(f"\n  === user {user.user_id}/{self.num_users} ===")
                print(f"    proxy: {user.proxy.proxy if user.proxy else '<none>'} "
                      f"({user.proxy.protocols if user.proxy else []})")
            tried: set[int] = set()
            for attempt in range(max_attempts_per_user):
                # Possibly rotate the arm if previous one failed
                if attempt > 0 or user.attempts == 0:
                    user.arm = self._pick_arm_for(user, tried)
                tried.add(id(user.arm))

                user.attempts += 1
                user.last_attempt_at = time.time()
                res = self._attempt_user(user)
                total_attempts += 1
                user.last_reward = res.reward
                user.last_detail = res.detail

                if verbose:
                    arm_str = f"fp={user.arm.fingerprint} prof={user.arm.profile_name}"
                    print(f"    [try {attempt + 1}/{max_attempts_per_user}] {arm_str}")
                    print(f"    reward={res.reward:+.2f}  {res.detail}")

                # Reward the bandit with this result
                self.bandit.update(
                    user.arm, res.reward,
                    blocked=res.blocked, errored=res.errored,
                    success=res.success,
                )
                if res.ok and res.user_key:
                    user.user_key = res.user_key
                    acquired += 1
                    if verbose:
                        print(f"    ✅ userKey: {res.user_key}")
                    break

                # short backoff
                time.sleep(0.4 + random.random() * 0.4)

            user.save()
            log_lines.append(json.dumps({
                "user_id": user.user_id,
                "proxy": user.proxy.proxy if user.proxy else None,
                "arm": user.arm.to_dict() if user.arm else None,
                "user_key": user.user_key,
                "attempts": user.attempts,
                "last_reward": user.last_reward,
                "detail": user.last_detail,
                "ts": time.time(),
            }))
            FARM_LOG_FILE.write_text("\n".join(log_lines) + "\n")
            self.bandit.save()
            FARM_STATE_FILE.write_text(json.dumps({
                "ts": time.time(),
                "num_users": self.num_users,
                "acquired": acquired,
                "users": [u.user_id for u in self.users if u.user_key],
            }, indent=2))

        duration = time.monotonic() - t_start
        summary = {
            "num_users": self.num_users,
            "acquired": acquired,
            "total_attempts": total_attempts,
            "duration_seconds": duration,
            "user_keys": [
                {
                    "user_id": u.user_id,
                    "proxy": u.proxy.proxy if u.proxy else None,
                    "user_key": u.user_key,
                }
                for u in self.users if u.user_key
            ],
        }
        if verbose:
            print()
            print(f"  === FARM SUMMARY ===")
            print(f"  users:    {self.num_users}")
            print(f"  acquired: {acquired}")
            print(f"  attempts: {total_attempts}")
            print(f"  duration: {duration:.1f}s")
        return summary


# =============================================================================
# Offline simulator (for testing the bandit without network)
# =============================================================================


class FarmSimulator:
    """
    Simulates the UserKeyFarm flow without network.
    Each (proxy × arm) combination has a hidden "true reward" probability.
    The bandit should learn which arms tend to work.

    This is what we'll use to demonstrate the algorithm in this sandbox.
    """

    def __init__(self, num_users: int = 20, seed: int = 42) -> None:
        self.num_users = num_users
        self.rng = random.Random(seed)
        self.bandit = perchance.PolicyLearner.load()
        # Simulated truth: only some (fp × profile) combinations "succeed"
        # In reality, this is determined by Cloudflare's bot detection;
        # here, we just bake in some structure.
        self.arms_truth: dict[tuple[str, str], float] = {}
        for arm in self.bandit.arms:
            # Favour chrome146 with chrome-macos, and safari on macos
            if arm.fingerprint == "chrome146" and arm.profile_name == "chrome-macos":
                self.arms_truth[(arm.fingerprint, arm.profile_name)] = 0.85
            elif arm.fingerprint == "safari184" and arm.profile_name == "safari-macos":
                self.arms_truth[(arm.fingerprint, arm.profile_name)] = 0.75
            elif arm.fingerprint == "chrome131" and arm.profile_name == "har-exact-chrome152-win":
                self.arms_truth[(arm.fingerprint, arm.profile_name)] = 0.60
            else:
                self.arms_truth[(arm.fingerprint, arm.profile_name)] = 0.05
        # Simulated proxy: 80% of proxies "work", 20% are dead
        self.proxy_alive = [self.rng.random() > 0.2 for _ in range(num_users)]

    def simulate(self, attempts_per_user: int = 5) -> dict:
        acquired = 0
        for user_id in range(1, self.num_users + 1):
            proxy_alive = self.proxy_alive[user_id - 1]
            for attempt in range(attempts_per_user):
                arm = self.bandit.select_arm()
                key = (arm.fingerprint, arm.profile_name)
                p_success = self.arms_truth[key] * (1.0 if proxy_alive else 0.0)
                got_key = self.rng.random() < p_success
                if got_key:
                    reward = perchance.REWARD_GOT_USERKEY
                elif not proxy_alive:
                    reward = perchance.REWARD_NETWORK_ERR
                else:
                    reward = perchance.REWARD_BLOCKED
                self.bandit.update(arm, reward,
                                   blocked=not got_key,
                                   errored=not proxy_alive,
                                   success=got_key)
                if got_key:
                    acquired += 1
                    break
            self.bandit.save()
        return {"acquired": acquired, "num_users": self.num_users}


# =============================================================================
# CLI
# =============================================================================


def _cmd_extract(args) -> int:
    db = find_proxy_miner_db()
    if db is None:
        print("  ERROR: could not find proxy-miner DB. Tried:")
        for p in PROXY_MINER_DB_CANDIDATES:
            print(f"    - {p}")
        return 1
    print(f"  using DB: {db}")
    proxies = extract_proxies(
        db,
        count=args.count,
        protocol=args.protocol,
        country_code=args.country,
        anonymity=args.anonymity,
        seed=args.seed,
    )
    if not proxies:
        print("  ERROR: no proxies matched the filters")
        return 1
    print(f"  extracted {len(proxies)} proxies from DB")
    out = Path(args.out) if args.out else PROXIES_TXT_FILE
    write_proxies_txt(proxies, out)
    # Also save the rich JSON version
    json_out = out.with_suffix(".json")
    json_out.write_text(json.dumps(
        {"count": len(proxies), "proxies": [p.to_dict() for p in proxies]},
        indent=2,
    ))
    print(f"  also wrote rich JSON: {json_out}")
    return 0


def _cmd_healthcheck(args) -> int:
    in_path = Path(args.in_) if args.in_ else PROXIES_TXT_FILE
    if not in_path.exists():
        print(f"  ERROR: input file not found: {in_path}")
        print(f"  Run: python3 {sys.argv[0]} extract first")
        return 1
    raw = [line.strip() for line in in_path.read_text().splitlines() if line.strip()]
    proxies: list[Proxy] = []
    for line in raw[:args.limit]:
        if ":" not in line:
            continue
        ip, port = line.rsplit(":", 1)
        try:
            port_int = int(port)
        except ValueError:
            continue
        proxies.append(Proxy(ip=ip, port=port_int, proxy=line,
                            protocols=["HTTP", "HTTPS"]))
    print(f"  health-checking {len(proxies)} proxies "
          f"(workers={args.workers}, timeout={args.timeout}s)")
    results = healthcheck_proxies(
        proxies, timeout=args.timeout, max_workers=args.workers,
    )
    out = Path(args.out) if args.out else LIVE_PROXIES_FILE
    live = write_live_proxies(proxies, results, out)
    print(f"\n  live: {len(live)} / {len(proxies)}  → {out}")
    return 0


def _cmd_farm(args) -> int:
    if not _CURL_CFFI_AVAILABLE:
        print("  ERROR: curl_cffi not installed. "
              "Run: pip install --break-system-packages curl_cffi")
        return 1
    in_path = Path(args.in_) if args.in_ else LIVE_PROXIES_FILE
    if not in_path.exists():
        print(f"  ERROR: {in_path} not found. Run healthcheck first.")
        return 1
    payload = json.loads(in_path.read_text())
    proxies = [Proxy(**{k: v for k, v in p.items() if k != "protocols"},
                     protocols=p.get("protocols", []))
               for p in payload["proxies"]]
    print(f"  loaded {len(proxies)} live proxies from {in_path}")
    farm = UserKeyFarm(
        proxies, num_users=args.users,
        client_timeout=args.timeout,
    )
    summary = farm.run(max_attempts_per_user=args.attempts)
    print(f"\n  acquired {summary['acquired']}/{summary['num_users']} userKeys")
    print(f"  per-user files in: {USERKEYS_DIR}")
    return 0


def _cmd_simulate(args) -> int:
    """Offline simulation: prove the bandit converges without network."""
    sim = FarmSimulator(num_users=args.users, seed=args.seed)
    print(f"  simulating {args.users} users × {args.attempts} attempts each …")
    res = sim.simulate(attempts_per_user=args.attempts)
    print(f"  acquired (simulated): {res['acquired']}/{res['num_users']}")
    print(f"\n  Top 5 arms the bandit learned to prefer:")
    for i, a in enumerate(sim.bandit.top_arms(5), 1):
        print(f"    {i}. fp={a.fingerprint:14s} prof={a.profile_name:25s} "
              f"n={a.n_trials:3d} success={a.success_rate:.1%} "
              f"mean_r={a.mean_reward:+.2f}")
    return 0


def _cmd_status(_args) -> int:
    print("=== UserKey Farm status ===")
    print(f"Cache dir:        {CACHE_DIR}")
    print(f"Proxies file:     {PROXIES_TXT_FILE} "
          f"({'exists' if PROXIES_TXT_FILE.exists() else 'missing'})")
    print(f"Live proxies:     {LIVE_PROXIES_FILE} "
          f"({'exists' if LIVE_PROXIES_FILE.exists() else 'missing'})")
    if LIVE_PROXIES_FILE.exists():
        d = json.loads(LIVE_PROXIES_FILE.read_text())
        print(f"  live count:     {d.get('live_count', '?')}")
    if USERKEYS_DIR.exists():
        files = sorted(USERKEYS_DIR.glob("user_*.json"))
        print(f"UserKey files:    {len(files)} in {USERKEYS_DIR}")
        for f in files[:5]:
            try:
                d = json.loads(f.read_text())
                uk = d.get("userKey")
                proxy = (d.get("proxy") or {}).get("proxy", "?")
                if uk:
                    print(f"  {f.name}: proxy={proxy:25s}  userKey={uk[:16]}…")
                else:
                    print(f"  {f.name}: proxy={proxy:25s}  (no userKey yet)")
            except Exception:
                pass
        if len(files) > 5:
            print(f"  … and {len(files) - 5} more")
    print(f"Farm state:       {FARM_STATE_FILE} "
          f"({'exists' if FARM_STATE_FILE.exists() else 'missing'})")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    parser = argparse.ArgumentParser(
        prog="multi_proxy.py",
        description="Multi-user userKey acquisition system.",
    )
    sub = parser.add_subparsers(dest="cmd")

    # extract
    p_ext = sub.add_parser("extract", help="Extract proxies from miner DB")
    p_ext.add_argument("--count", type=int, default=50)
    p_ext.add_argument("--protocol", help="Filter: HTTP/HTTPS/SOCKS4/SOCKS5")
    p_ext.add_argument("--country", help="Filter: US, DE, IN, …")
    p_ext.add_argument("--anonymity", help="Filter: Elite/Anonymous/Transparent")
    p_ext.add_argument("--seed", type=int)
    p_ext.add_argument("--out", help="Output path (default: ~/.perchance_client/proxies.txt)")
    p_ext.set_defaults(func=_cmd_extract)

    # healthcheck
    p_hc = sub.add_parser("healthcheck", help="Verify proxies actually work")
    p_hc.add_argument("--in", dest="in_", help="Input proxies.txt")
    p_hc.add_argument("--out", help="Output live_proxies.json")
    p_hc.add_argument("--workers", type=int, default=20)
    p_hc.add_argument("--timeout", type=float, default=8.0)
    p_hc.add_argument("--limit", type=int, default=200,
                      help="Only check the first N proxies")
    p_hc.set_defaults(func=_cmd_healthcheck)

    # farm
    p_farm = sub.add_parser("farm", help="Farm userKeys (one per user, one per proxy)")
    p_farm.add_argument("--users", type=int, default=20)
    p_farm.add_argument("--in", dest="in_", help="Input live_proxies.json")
    p_farm.add_argument("--out", help="Output directory for userKey files")
    p_farm.add_argument("--attempts", type=int, default=5)
    p_farm.add_argument("--timeout", type=float, default=20.0)
    p_farm.set_defaults(func=_cmd_farm)

    # simulate
    p_sim = sub.add_parser("simulate", help="Offline simulation of the bandit")
    p_sim.add_argument("--users", type=int, default=20)
    p_sim.add_argument("--attempts", type=int, default=5)
    p_sim.add_argument("--seed", type=int, default=42)
    p_sim.set_defaults(func=_cmd_simulate)

    # status
    p_stat = sub.add_parser("status", help="Show farm state")
    p_stat.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv[1:])
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
