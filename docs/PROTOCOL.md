# Perchance image-generation protocol (from `perchance.org.json`)

Captured 2026-08-25 by Chrome 152 DevTools (HAR 1.2, 1162 entries) on
`https://perchance.org/imageapi?prompt=a%20cute%20booy`.

Response **bodies** for `image-generation.perchance.org/api/*` were stripped
by DevTools (`content.size > 0`, `text` empty). URLs, query strings, methods,
and the generate POST body are intact — that is enough to rebuild the flow.

## Identity model

There is **no cookie** on `/api/generate`. Identity is:

1. `userKey` — 64 lowercase hex chars, issued per IP by `verifyUser`
2. the **source IP** (the proxy). The key is sticky to that IP.
3. `adAccessCode` — 64 hex chars from `perchance.org/api/getAccessCodeForAdPoweredStuff`

Therefore: **one Chromium instance ↔ one proxy ↔ one userKey**. Never mix.

## Page bootstrap

```
GET https://perchance.org/imageapi?prompt=…
  └─ iframe  https://{hash}.perchance.org/imageapi?__generatorLastEditTime=…
       └─ many iframes  https://image-generation.perchance.org/embed#…
```

Cloudflare (`cf-ray`, `cdn-cgi/challenge-platform`) sits on both origins.
That is why minting uses DrissionPage Chromium, not a raw HTTP client.

## Endpoints (observed counts)

| method | host | path | count |
| --- | --- | --- | --- |
| GET | image-generation.perchance.org | `/api/verifyUser` | 60 |
| POST | image-generation.perchance.org | `/api/generate` | 70 |
| GET | image-generation.perchance.org | `/api/getUserQueuePosition` | 54 |
| GET | image-generation.perchance.org | `/api/awaitExistingGenerationRequest` | 40 |
| GET | image-generation.perchance.org | `/api/downloadTemporaryImageViaProxy` | 29 |
| GET | image-generation.perchance.org | `/embed` | 30 |
| GET | perchance.org | `/api/getAccessCodeForAdPoweredStuff` | 1 |

### 1. Mint userKey

```
GET https://image-generation.perchance.org/api/verifyUser?thread=0&__cacheBust={random}
Referer: https://image-generation.perchance.org/embed
```

Response ~106 bytes JSON (body missing in HAR). Parser accepts:

```json
{"status":"success","userKey":"<64 hex>"}
{"status":"already_verified","userKey":"<64 hex>"}
```

or a bare 64-hex string. Observed key:

`fe14a03dabcc4c3013f7f56db38bc7cda58b0da258fcef96b1c10afe08d5aa7e`

### 2. Ad access code

```
GET https://perchance.org/api/getAccessCodeForAdPoweredStuff?__cacheBust=2979450
Referer: https://perchance.org/imageapi?prompt=…
```

Response is **plain text** hex (not JSON):

`bdb81e510ad8273f27163560c1280622ecd6df7eabe87a7a8d4a94411812a843`

### 3. Generate

```
POST https://image-generation.perchance.org/api/generate
     ?userKey=…&requestId=0.5295…&adAccessCode=…&__cacheBust=…
Content-Type: text/plain;charset=UTF-8
Origin: https://image-generation.perchance.org
Referer: https://image-generation.perchance.org/embed
```

JSON body (also sent as text/plain):

```json
{
  "prompt": "a cute booy",
  "negativePrompt": "",
  "seed": -1,
  "resolution": "512x768",
  "guidanceScale": 7,
  "channel": "imageapi",
  "subChannel": "public",
  "userKey": "fe14a03d…aa7e",
  "adAccessCode": "bdb81e51…a843",
  "requestId": "0.5295441559728221"
}
```

Valid resolutions observed/documented by the plugin: `512x512`, `512x768`,
`768x512`, `768x768`. `channel` is the generator name (`imageapi`).

### 4. Queue + await

```
GET /api/getUserQueuePosition?userKey=…&requestId=…
GET /api/awaitExistingGenerationRequest?userKey=…&__cacheBust=…
```

### 5. Download

```
GET /api/downloadTemporaryImageViaProxy?t=v1.…
GET /api/downloadTemporaryImage?imageId=…
```

`Content-Type: image/jpeg`.

## Proxy control plane

Hugging Face Space wrapping [RepopoxRev/proxy-miner](https://github.com/Adarshukumar/RepopoxRev/tree/main/proxy-miner):

```
https://adarshu07-no-plz.hf.space
```

| method | path | role |
| --- | --- | --- |
| GET | `/api/health` | uptime, db count |
| GET | `/api/proxies?protocol=HTTP&sort=delay&order=asc&limit=50` | JSON list |
| GET | `/api/proxies/raw` | `ip:port` lines |
| GET | `/api/stats` | aggregates |

Every Chromium and every `curl_cffi` session sets **that one proxy** and
never rebinds it.
