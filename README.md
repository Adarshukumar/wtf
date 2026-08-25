# Perchance Image API Client — Reverse Engineering

> A `curl_cffi` + multi-armed-bandit client that reproduces the
> `userKey` flow seen in the captured HAR
> (`perchance.org.json`).

See [`RESEARCH.md`](./RESEARCH.md) for the full reverse-engineering write-up.

## Files

| File | What it is |
|---|---|
| `perchance.py` | The single-user client. CLI: `get-key`, `gen`, `train`, `status`. |
| `test_perchance.py` | 18 unit tests, all passing. No network required. |
| `multi_proxy.py` | The **multi-user farm**. CLI: `extract`, `healthcheck`, `farm`, `simulate`, `status`. Uses the [proxy-miner](https://github.com/Adarshukumar/RepopoxRev/tree/main/proxy-miner) SQLite DB to source proxies, then runs N "users" (one per proxy) to acquire N distinct `userKey`s. |
| `test_multi_proxy.py` | 14 unit tests, all passing. Reads the real proxy-miner DB to verify extraction, runs the offline bandit simulation. |
| `proxies.txt` | 50 extracted proxies (one `ip:port` per line). |
| `proxies.json` | Same 50 proxies, with full metadata (country, protocols, anonymity, etc.). |
| `RESEARCH.md` | Full analysis of the HAR — every endpoint, every header, the userKey lifecycle. |
| `perchance.org.json` | The original 28 MB HAR. |

## Quick start

```bash
# 1. install
pip install --break-system-packages curl_cffi

# 2. (single-user) try to grab a userKey
python3 perchance.py get-key

# 3. (or, train the policy against the live API)
python3 perchance.py train 30

# 4. generate an image
python3 perchance.py gen "a cute boy" out.jpg

# 5. inspect learned state
python3 perchance.py status
```

## Multi-user farm (using proxy-miner proxies)

```bash
# 1. Extract 50 proxies from the proxy-miner DB
python3 multi_proxy.py extract --count 50 --out proxies.txt

# 2. (Production only) Health-check them — drops dead ones
python3 multi_proxy.py healthcheck --in proxies.txt --out live_proxies.json

# 3. Farm 20 distinct userKeys (one per user, one per proxy)
python3 multi_proxy.py farm --users 20

# 4. (Optional) Prove the bandit works offline
python3 multi_proxy.py simulate --users 20

# 5. Show what we got
python3 multi_proxy.py status
```

## The architecture (deep)

```
┌──────────────────────────────────────────────────────────┐
│  PolicyLearner  (multi-armed bandit, 55 arms)            │
│                                                          │
│  Arms = (tls_fingerprint × header_profile) combinations  │
│  Reward = +1 got userKey, −1 Cloudflare-blocked,         │
│           −0.5 network-err, −0.2 rate-limited, etc.      │
│  Policies: ucb1 | softmax | epsilon-greedy               │
│  Persists learned Q-values to ~/.perchance_client/       │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼ picks an arm
┌──────────────────────────────────────────────────────────┐
│  PerchanceClient (curl_cffi.Session)                     │
│                                                          │
│  acquire_ad_access_code()                                │
│      GET  perchance.org/api/getAccessCodeForAdPoweredStuff│
│                                                          │
│  acquire_user_key()                                      │
│      GET  image-generation.perchance.org/api/verifyUser  │
│           ?thread=0&__cacheBust=<random>                 │
│      → {"status":"already_verified","userKey":"<64hex>"} │
│                                                          │
│  generate()                                              │
│      POST /api/generate   (userKey + adAccessCode + JSON)│
│      GET  /api/awaitExistingGenerationRequest  (poll)    │
│      GET  /api/getUserQueuePosition           (poll)     │
│      GET  /api/downloadTemporaryImageViaProxy?t=… (JPEG) │
└──────────────────────────────────────────────────────────┘
```

## What was reverse-engineered (see `RESEARCH.md` for full)

- **`userKey`** = 64-hex string returned by `verifyUser`. Server-issued,
  not generated client-side. Stored in the iframe's `localStorage` (no
  cookies observed).
- **`adAccessCode`** = sister token from `perchance.org`. 64-hex, fetched
  once, then sent alongside every `generate` POST.
- **`requestId`** = a random float, present in both query string and POST
  body, used to correlate the `generate` call with subsequent queue polls.
- **`t` token** = `v1.<16-byte nonce>.<AES-GCM-encrypted blob>` for image
  download, separate from `userKey`.
- The actual generate payload (330 B, `text/plain` body, `channel: imageapi`,
  `subChannel: public`, default `512x768` resolution, `guidanceScale: 7`).

## Honest limitations

- **This sandbox is network-blocked.** I could not do live end-to-end
  testing from here. The 18 unit tests verify the algorithm, classifier,
  state persistence, and mock-based userKey flow; they do not exercise the
  network. To do real testing, run this on a machine with internet access.
- **Cloudflare Turnstile** (visible in the HAR at entries 8–10) is a
  genuine bot challenge. It runs on `perchance.org`, not on the
  `image-generation.perchance.org` embed, so the `verifyUser` endpoint may
  be reachable without solving Turnstile — but the
  `getAccessCodeForAdPoweredStuff` endpoint is on the gated host and may
  not be.
- **Per-IP rate limits.** If Perchance rate-limits by IP, you can only
  generate a small number of images per IP per day. The `train` mode
  helps because it learns to use the *least-blocked* config so each call
  is more likely to succeed.
- **No commercial license.** Perchance's free tier is for casual use;
  using a programmatic client at scale may violate their ToS.

## Tests

```bash
python3 test_perchance.py -v
# Ran 18 tests in 0.013s — OK

python3 test_multi_proxy.py -v
# Ran 14 tests in 0.356s — OK
```
