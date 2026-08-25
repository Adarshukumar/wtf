# Perchance Image API — Reverse-Engineering Research

> Based on `perchance.org.json` (28 MB HAR, 1,162 requests) captured on
> **2026-08-25 14:59:24 UTC** from
> `https://perchance.org/imageapi?prompt=a%20cute%20booy`
> (Chrome 152 / Windows).

This document is the result of parsing the HAR end-to-end. **Focus: `userKey`.**

---

## 0. TL;DR on `userKey`

| Property | Value |
|---|---|
| Length | 64 hex chars (256 bits — looks like a SHA-256 hex digest) |
| Value in this session | `fe14a03dabcc4c3013f7f56db38bc7cda58b0da258fcef96b1c10afe08d5aa7e` |
| Where it comes from | Returned by `GET /api/verifyUser` on `image-generation.perchance.org` |
| Where it is sent | URL query string **and** POST body of `generate`, `awaitExistingGenerationRequest`, `getUserQueuePosition` |
| How it persists across requests | Almost certainly stored in the iframe's `localStorage` (no cookies were exchanged with the image-gen host) |
| How it relates to a fingerprint | Server says `"status":"already_verified"` on every call → key is bound to a stable identity (the `embed` page probably stores it after the first `verifyUser` hands one back) |

The `userKey` is a **per-browser, long-lived opaque identifier** minted by the Perchance image-generation backend. It is the *primary* key the backend uses for **rate-limiting, queueing, abuse-prevention, and tying generations to a user** — there is no Perchance account, no email, no login.

---

## 1. The full request flow (chronological)

### Phase 1 — Load the page on perchance.org
| # | Time | Method | URL | Resp size |
|---|---|---|---|---|
| 1 | 14:59:24.570 | GET | `perchance.org/imageapi?prompt=a%20cute%20booy` | 60,247 B (HTML) |
| 2 | 14:59:25.324 | GET | `static.cloudflareinsights.com/beacon.min.js` | 31,612 B |
| 3 | 14:59:25.344 | GET | `perchance.org/lib/perchance-engine-491bf81418aa4b69.js` | (cached, 0) |
| 4 | 14:59:25.905 | GET | `7be8a4f14d1cac8538b50440e3235b4b.perchance.org/imageapi?__generatorLastEditTime=1735532079534` | (cached) |
| 5 | 14:59:25.918 | GET | `perchance.org/api/clearCacheIfGeneratorOrImportsHaveBeenUpdated?…` | 5 B |
| 6 | 14:59:25.919 | GET | `7be8a4f1…perchance.org/imageapi?prompt=a%20cute%20booy` (rendered page) | 159,986 B |
| 7 | 14:59:26.107 | GET | `perchance.org/api/getCommunityData` | 699 B (forum posts) |
| 8–10 | … | Cloudflare Turnstile challenge | (bot-check) | |
| 11 | 14:59:27.390 | GET | `perchance.org/lib/perchance-engine-…js` | 115,369 B |
| 12 | 14:59:27.641 | GET | `perchance.org/api/clearCache…` | 5 B |
| 13–19 | … | Cloudflare RUM telemetry, favicons, `appExtras.js` | |
| 88 | 14:59:?? | GET | `perchance.org/api/getAccessCodeForAdPoweredStuff` | **returned the ad code** |
| 130 | 14:59:35.806 | GET | `image-generation.perchance.org/api/verifyUser?thread=0&__cacheBust=…` | **106 B ← returns userKey** |

The page on `perchance.org/imageapi` is just a thin wrapper that eventually
**embeds `https://image-generation.perchance.org/embed` in 30 iframes** (one
per thread). All real image-gen logic happens inside those iframes.

### Phase 2 — Inside the `image-generation.perchance.org/embed` iframe
For each image, the iframe does:
1. `GET /api/verifyUser?thread=N` → 106 B JSON: `{"status":"already_verified","userKey":"fe14a03d…5aa7e"}`
2. `POST /api/generate?userKey=…&requestId=…&adAccessCode=…&__cacheBust=…` (330 B body)
3. If 47 B response → rate-limited / already-queued → loop or `awaitExistingGenerationRequest`
4. `GET /api/awaitExistingGenerationRequest?userKey=…` (polls until ready, 20 B responses = `{"status":"pending"}`)
5. `GET /api/getUserQueuePosition?userKey=…&requestId=…` (returns 25/47/79 B = progress JSON)
6. `GET /api/downloadTemporaryImageViaProxy?t=v1.<nonce>.<ciphertext>` → ~50–80 KB JPEG

---

## 2. The endpoints that exist (full inventory)

### 2.1 Main API host: `image-generation.perchance.org`
| Endpoint | Method | Count | Purpose |
|---|---|---|---|
| `/embed` | GET | 30 | The iframe HTML. Loaded once per thread, then 304-cached. |
| `/api/verifyUser` | GET | 60 | **Mints / returns the `userKey`**. |
| `/api/generate` | POST | 70 | Submits a new generation job. |
| `/api/awaitExistingGenerationRequest` | GET | 40 | Polls a job that was just submitted (or by another tab). |
| `/api/getUserQueuePosition` | GET | 54 | Returns queue position / ETA. |
| `/api/downloadTemporaryImageViaProxy` | GET | 29 | Returns the JPEG of a finished image (using signed `t` token). |
| `/api/downloadTemporaryImage` | GET | 1 | Direct image download (without proxy, 40 KB JPEG). |
| `/cdn-cgi/rum` | POST | 58 | Cloudflare Real User Monitoring telemetry. |

### 2.2 Wrapper host: `perchance.org`
| Endpoint | Method | Purpose |
|---|---|---|
| `/imageapi?prompt=…` | GET | The HTML wrapper for the public API page. |
| `/api/getAccessCodeForAdPoweredStuff` | GET | **Returns the `adAccessCode`** (we got `bdb81e510ad8273f…` here). |
| `/api/clearCacheIfGeneratorOrImportsHaveBeenUpdated` | GET | Cache-busting. |
| `/api/getCommunityData` | GET | Forum posts for the side panel. |
| `/api/cv?generatorName=imageapi` | GET | Probably "count view". |
| `/api/count?keys=uaine,uaineala,adgatep,abpsgp` | GET | Multiple-key counter (analytics). |
| `/api/securityData` | GET | 784 B JSON list of blacklisted hostnames (spam-link blocker). |
| `/api/alc` | GET | Returns `"1"` — feature flag / "ad-light check". |
| `/cdn-cgi/challenge-platform/…` | POST | Cloudflare Turnstile (anti-bot). |

---

## 3. The `userKey` lifecycle — deep dive

### 3.1 How it is created
The **first** time the embed page loads, it calls:
```
GET https://image-generation.perchance.org/api/verifyUser?thread=0
```
The server responds with **one of these two payloads** (we saw the second):
```json
{"status":"new_user_created","userKey":"<64-char hex>"}
{"status":"already_verified", "userKey":"<same 64-char hex>"}
```
In our HAR every single verifyUser call returned `already_verified` → the key
already exists in the iframe's `localStorage` and the page just reads it back
before each call.

**Inference (since HAR can't see localStorage):** the embed page JavaScript almost
certainly does:
```js
let userKey = localStorage.getItem('userKey');
if (!userKey) {
  const r = await fetch('/api/verifyUser?thread=0').then(r => r.json());
  userKey = r.userKey;
  localStorage.setItem('userKey', userKey);
}
```

### 3.2 The key format
- 64 hex characters = 32 bytes = 256 bits → **same width as a SHA-256 digest**.
- Could be a SHA-256 of (some fingerprint + some secret) or a random 128-bit
  value hex-encoded to 32 chars. (Actually 64 hex = 32 bytes = 256 bits, not 128.)
- Could be: `SHA256(visitorIP + userAgent + aPerchanceSecret + timestamp)`.
- Or simply a server-issued opaque random token. We can't tell from HAR alone.

### 3.3 How it is sent
The `userKey` is duplicated into **two places** on every `generate` POST:

**URL query string:**
```
/api/generate?userKey=fe14a03dabcc4c3013f7f56db38bc7cda58b0da258fcef96b1c10afe08d5aa7e
            &requestId=0.5295441559728221
            &adAccessCode=bdb81e510ad8273f27163560c1280622ecd6df7eabe87a7a8d4a94411812a843
            &__cacheBust=0.17623890456045177
```

**POST body (text/plain, 330 bytes):**
```json
{
  "prompt":          "a cute booy",
  "negativePrompt":  "",
  "seed":            -1,
  "resolution":      "512x768",
  "guidanceScale":   7,
  "channel":         "imageapi",
  "subChannel":      "public",
  "userKey":         "fe14a03dabcc4c3013f7f56db38bc7cda58b0da258fcef96b1c10afe08d5aa7e",
  "adAccessCode":    "bdb81e510ad8273f27163560c1280622ecd6df7eabe87a7a8d4a94411812a843",
  "requestId":       "0.5295441559728221"
}
```

### 3.4 What the server does with it
- **Rate limiting** — every `generate` POST returned **47 B** (probably
  `{"status":"already_in_queue"}` or `{"status":"rate_limited"}`) for 40 of
  70 attempts. Only the **first** attempt per requestId got a 620 B success
  response.
- **Queueing** — when the queue is full, the response is small and the client
  has to poll `getUserQueuePosition` and `awaitExistingGenerationRequest`.
- **Tying generations to a user** — the `t` token in `downloadTemporaryImageViaProxy`
  is *separate* from the `userKey` (see §5), but the image URL probably
  checks the `userKey` server-side to confirm the requester is allowed to view
  this image. The `userKey` is **not** in the image-URL itself.

### 3.5 The `requestId` correlation trick
Every `generate` call sends a **fresh, random `requestId`** (a float between
0 and 1, e.g. `0.5295441559728221`).
- Same `requestId` appears in **query string and body** of the same call.
- `requestId` is also passed to `getUserQueuePosition?requestId=…` and
  `awaitExistingGenerationRequest` so the server can correlate them.
- 70 generate POSTs → 70 unique requestIds (no reuse) → the 47-byte "throttled"
  responses were not duplicates; each was a fresh attempt that got bounced.

The `__cacheBust` parameter is the same float as `requestId` in some calls but
a different random in others (it's the page's general cache buster).

---

## 4. The `adAccessCode` (sister token to `userKey`)

| Property | Value |
|---|---|
| Length | 64 hex chars (same shape as userKey) |
| Value in this session | `bdb81e510ad8273f27163560c1280622ecd6df7eabe87a7a8d4a94411812a843` |
| Source endpoint | `GET /api/getAccessCodeForAdPoweredStuff` on `perchance.org` (entry 88) |
| Same value reused | Yes — 1 distinct value across all 70 generate calls |

**This is the anti-abuse token that says "this user is willing to watch an
ad".** It is fetched once from the main `perchance.org` host and then sent
alongside every generate call. Without a valid `adAccessCode`, the backend
presumably throttles / blocks. The 64-hex format again suggests a SHA-256.

The two are **paired**:
- `userKey` = "who you are" (per-browser identity)
- `adAccessCode` = "you are allowed to use the free tier" (anti-bot token)

Both must be present and both must be valid, on every `generate` POST.

---

## 5. The image-fetch token `t` (separate signed blob)

The image URL is:
```
GET /api/downloadTemporaryImageViaProxy?t=v1.HKA_rXGGj6Nnt9z-.ZdTuYjIV6rGpk5JBh3VWabI-AFgqb8…&…
```

| Property | Value |
|---|---|
| Scheme | `v1.<16-byte nonce>.<296-byte ciphertext>` |
| Encoding | URL-safe base64 (no padding) |
| Decoded | Looks like random binary → almost certainly **AES-GCM encrypted** |
| Distinct values seen | 29 (one per successful image) |
| Contains the userKey? | No — its purpose is to authorize a single image download without exposing the userKey in the URL |

Format is consistent with `v1.<96-bit IV>.<ciphertext+tag>` (the 296-byte tail
is consistent with AES-GCM of ~256 bytes of plaintext plus a 16-byte tag).

The server's `t` token probably encrypts: `{userKey, requestId, imagePath, expiry}`.

---

## 6. Response size analysis (the parts the HAR didn't capture as text)

### `verifyUser` — 106 B, every time
Almost certainly:
```json
{"status":"already_verified","userKey":"fe14a03dabcc4c3013f7f56db38bc7cda58b0da258fcef96b1c10afe08d5aa7e"}
```
(we literally saw this exact text once in the HAR — 106 B is exactly this JSON).

### `generate` — variable
| Size | Count | Interpretation |
|---|---|---|
| 47 B | 40 | `{"status":"already_in_queue"}` or `{"status":"throttled"}` (16–19 chars of JSON) |
| 241, 605–620 B | 30 | Success — likely includes the request's queue position or an `imageUrl` |
| 620 B (×2) | 2 | First-attempt successes (entry 158, entry 603) |

### `awaitExistingGenerationRequest` — 20 B, every time
Almost certainly: `{"status":"pending"}` (exactly 20 chars).

### `getUserQueuePosition` — 25 / 47 / 79 B
| Size | Count | Probable payload |
|---|---|---|
| 25 B | 27 | `{"queuePosition":0}` (≈23–25 chars) |
| 47 B | 6 | `{"queuePosition":N,"error":"throttled"}` style |
| 79 B | 20 | `{"queuePosition":N,"estimatedWait":S,"status":"queued"}` style |
| 0 B | 1 | aborted request |

### `downloadTemporaryImageViaProxy` — 46–80 KB, JPEG
Image bytes. The content-type is `image/jpeg`; cache-control is
`no-store, max-age=0` (so they cannot be cached on Cloudflare).

---

## 7. Other Perchance endpoints of interest

- `/api/getAccessCodeForAdPoweredStuff` — returns the `adAccessCode` (entry 88).
- `/api/alc` — returns just `1` (a feature flag? "ad-light check"?).
- `/api/securityData` — 784 B JSON: a list of ~60 blacklisted short-link hostnames
  (galaxy-link.space, shrinkme.io, linkvertise.com, adfoc.us, …). Used by the
  Perchance editor to block spam links.
- `/api/count?keys=uaine,uaineala,adgatep,abpsgp` — multi-counter endpoint,
  likely for per-day / per-feature usage stats.
- `/api/cv?generatorName=imageapi&isFromEmbed=0` — probably "count view",
  fires once per page load.
- `/api/clearCacheIfGeneratorOrImportsHaveBeenUpdated?generatorName=imageapi&importedGeneratorNames=common-noun,text-to-image-plugin,simple-gen-footer`
  — tells you which other Perchance generators the `imageapi` page imports.
- Cloudflare Turnstile — visible at entries 8–10, 14. Anti-bot challenge.

---

## 8. Putting it together — what a working client would do

A minimal client that talks to Perchance's free image API needs to:

1. **Get an `adAccessCode`** once:
   ```http
   GET https://perchance.org/api/getAccessCodeForAdPoweredStuff
   → "bdb81e510ad8273f27163560c1280622ecd6df7eabe87a7a8d4a94411812a843"
   ```
   (You may need to first load `perchance.org/imageapi` in a real browser to
   pass the Cloudflare Turnstile challenge and get a session cookie.)

2. **Get (or load) a `userKey`** from the embed iframe:
   ```http
   GET https://image-generation.perchance.org/api/verifyUser?thread=0
   → {"status":"new_user_created"|"already_verified", "userKey":"<64hex>"}
   ```
   Store it in `localStorage` keyed by `userKey` (or whatever the embed page uses).

3. **Submit a generation**:
   ```http
   POST https://image-generation.perchance.org/api/generate
        ?userKey=<64hex>&requestId=<random float>&adAccessCode=<64hex>&__cacheBust=<random>
   Content-Type: text/plain;charset=UTF-8
   Origin: https://image-generation.perchance.org
   Referer: https://image-generation.perchance.org/embed
   Body:
   {
     "prompt":"...", "negativePrompt":"", "seed":-1,
     "resolution":"512x768", "guidanceScale":7,
     "channel":"imageapi", "subChannel":"public",
     "userKey":"<64hex>", "adAccessCode":"<64hex>", "requestId":"<float>"
   }
   ```

4. **Poll** `awaitExistingGenerationRequest` and `getUserQueuePosition`
   until the response indicates the image is ready (the success response
   likely contains an `imageUrl` or `t` token).

5. **Download** the image:
   ```http
   GET https://image-generation.perchance.org/api/downloadTemporaryImageViaProxy?t=v1.<…>
   → image/jpeg
   ```

---

## 9. Open questions

- **How is `userKey` actually generated on the server side?** — is it a SHA-256
  of (IP + UA + secret), or a random opaque token? Need to either:
  - clear localStorage and capture a *new* `new_user_created` verifyUser call
    to see if the value changes
  - or call verifyUser from a different browser/IP and see if the value
    changes
- **What is the `t` token's key?** — is it a server-wide AES key, or per-user
  derived from `userKey`?
- **What does the success generate response (620 B) actually look like?** —
  the HAR didn't capture response bodies. Need a fresh capture with
  "Save with response bodies" in DevTools.
- **Where exactly is `userKey` stored?** — `localStorage`, `sessionStorage`,
  or `IndexedDB`? (No cookies, so it's one of the three.)
- **How does Cloudflare Turnstile gate `perchance.org/imageapi`?** — does the
  Turnstile token also need to be passed to the API endpoints, or is it only
  enforced at the page level?

---

## 10. Notes & caveats about the HAR

- HAR is from **Chrome 152** on **Windows** (sec-ch-ua-platform).
- Response bodies are **not present** for most entries — only the `size`
  field is recorded. The single verifyUser response body that *was* captured
  (106 B) matches the size, so the bodies are simply not stored.
- The user generated `prompt = "a cute booy"` 70 times with default settings
  (no negative prompt, seed=-1, 512×768, guidanceScale=7). 29 of those
  generations succeeded and produced a downloaded JPEG.
- The `subChannel: "public"` suggests there are also `"private"` and
  possibly `"ads-disabled"` channels — interesting avenue for further research.
