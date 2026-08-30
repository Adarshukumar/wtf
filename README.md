# Perchance userKey + generate

Two jobs, two libraries, one optional proxy:

| step | tool | what |
| --- | --- | --- |
| mint `userKey` | **DrissionPage** (real Chrome) | load `/embed`, **listen** until `/api/verifyUser` returns `success` or `already_verified` |
| generate image | **curl_cffi** | same IP/proxy, no Chrome |

Proxies are **fetched** from the miner API. We do not run the miner.

```
https://adarshu07-no-plz.hf.space/api/proxies
```

`--no-proxy` skips that and talks to Perchance directly.

## What the HARs show (this is the whole trick)

`generator.har` — first visit, user is **not** verified yet:

```
GET /api/verifyUser?thread=0
    {"status":"failed_verification","reason":"token_required"}   ×15

Cloudflare Turnstile (sitekey 0x4AAAAAAAA8g8NphwaSOT59)

GET /api/verifyUser?token=1.<turnstile>&thread=0
    {"status":"success","userKey":"2c9aff54…cb8b04"}

GET /api/verifyUser?thread=0
    {"status":"already_verified","userKey":"2c9aff54…cb8b04"}
```

`prompt.har` — later visit, same key already good:

```
GET /api/checkUserVerificationStatus?userKey=2c9aff54…   (21 bytes = {"status":"verified"})
POST /api/generate   channel=imageapi  adAccessCode=<64 hex>
    {"status":"success","imageId":"…","imageDownloadUrl":"/api/downloadTemporaryImageViaProxy?t=…"}
    or {"status":"waiting_for_prev_request_to_finish"}
```

Chrome is required because Turnstile has to run. Headless usually fails it — run headed under Xvfb.

The key is **IP-sticky**. Mint and generate on the same proxy (or both direct).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Chrome/Chromium on PATH. Optional: xvfb-run for a virtual display.
```

## Commands

```bash
# mint a key — picks one HTTP proxy from the API
python -m perchance extract

# mint with no proxy
python -m perchance extract --no-proxy

# mint through a proxy you already have
python -m perchance extract --proxy http://1.2.3.4:8080

python -m perchance proxy-list --protocol HTTP

# generate (curl_cffi). Reuses the proxy stored with the last key.
python -m perchance generate --prompt "a cute cat"

python -m perchance generate --prompt "a cute cat" --no-proxy --key <hex>

# both steps, one proxy
python -m perchance run --prompt "a cute cat"
python -m perchance run --prompt "a cute cat" --no-proxy
```

Keys append to `data/keys.jsonl`. Images go to `output/image.jpg`.

```bash
# headed Chrome on a machine with no desktop:
xvfb-run -a python -m perchance extract --no-proxy
```

## Tests (offline, against the HARs)

```bash
pip install pytest
python -m pytest -q
```
