# wtf — Perchance userKey extractor

Mint a Perchance `userKey` the way the real site does it: **Chromium
(DrissionPage) behind one proxy**, then talk to the image API with
**curl_cffi** on that same proxy.

The captured Chrome HAR `perchance.org.json` is the source of truth for
the protocol. Walkthrough: [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Why this shape

| piece | job |
| --- | --- |
| Hugging Face proxy miner | `https://adarshu07-no-plz.hf.space` — pool of HTTP/HTTPS/SOCKS5 |
| DrissionPage | pass Cloudflare, load `/embed`, call `/api/verifyUser` |
| curl_cffi | Chrome TLS fingerprint for generate / queue / download |
| SQLite `data/keys.sqlite` | every key stored **with** the proxy that minted it |

`userKey` is IP-sticky. Mixing proxies after minting burns the key.

```
proxy-miner API  ──►  pick ONE proxy
                         │
                         ▼
              Chromium (DrissionPage)
              GET image-generation.perchance.org/embed
              GET /api/verifyUser  ──► userKey
                         │
                         ▼
              curl_cffi Session(proxy=SAME)
              GET /api/getAccessCodeForAdPoweredStuff
              POST /api/generate
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Chromium/Chrome must be on `PATH` (or set `CHROME_PATH`). Headless is on
by default; set `PERCHANCE_HEADLESS=0` if Cloudflare challenges you.

```bash
export PERCHANCE_PROXY_API=https://adarshu07-no-plz.hf.space
```

## Commands

```bash
# reconstruct the protocol from the HAR (no network)
python -m perchance_key analyze-har

# control plane
python -m perchance_key proxy-health
python -m perchance_key proxy-list --protocol HTTP --limit 20

# mint N keys; each key gets a fresh proxy + its own Chrome profile
python -m perchance_key extract -n 1
python -m perchance_key extract -n 3 --protocol HTTP --country US

# pin a proxy yourself
python -m perchance_key extract --proxy http://1.2.3.4:8080

python -m perchance_key keys
python -m perchance_key generate --prompt "a cute cat"
```

## Tests

```bash
pip install pytest
python -m pytest -q
```

`tests/test_har.py` replays the 29 MB HAR and asserts the observed
userKey, adAccessCode, generate body, and endpoint set.

## Layout

```
perchance_key/
  config.py       origins, env
  proxy_api.py    HF space client
  browser.py      DrissionPage + proxy
  extractor.py    mint userKey
  http.py         curl_cffi BoundSession
  pipeline.py     one proxy → one key
  store.py        sqlite
  har.py          HAR → protocol facts
  parse.py        userKey / adAccessCode
  cli.py
docs/PROTOCOL.md
perchance.org.json
```
