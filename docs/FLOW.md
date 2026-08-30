# Flow reconstructed from generator.har + prompt.har

## Mint (DrissionPage) — same URL as the HAR

1. Chromium, optional `--proxy-server`.
2. `GET https://perchance.org/imageapi?prompt=a%20cute%20booy`
   That is the captured page. It then loads:
   - `https://{hash}.perchance.org/imageapi?…&prompt=…` (sandbox iframe)
   - many `https://image-generation.perchance.org/embed#…` iframes
3. Listen on the tab (covers iframe traffic):
   - ignore `{"status":"failed_verification","reason":"token_required"}`
   - Turnstile runs **inside the embed iframe**
   - accept `{"status":"success","userKey":…}` or `already_verified`
   - also catch `userKey=` on `/api/generate` and `/api/checkUserVerificationStatus`
   - parent origin: `GET /api/getAccessCodeForAdPoweredStuff` → adAccessCode
4. Close Chrome. Image download is curl_cffi, not Chrome.

`userKey` is stored on the **embed origin** `localStorage['userKey-'+thread]`, not on perchance.org.

## Generate (curl_cffi, same IP)

```
POST https://image-generation.perchance.org/api/generate
     ?userKey=&requestId=&adAccessCode=&__cacheBust=
Content-Type: text/plain;charset=UTF-8
Origin/Referer: image-generation.perchance.org/embed

{"prompt","negativePrompt","seed":-1,"resolution","guidanceScale":7,
 "channel":"imageapi","subChannel":"public","userKey","adAccessCode","requestId"}
```

`channel` was `imageapi` in prompt.har and `ai-text-to-image-generator` in generator.har.

Then `GET imageDownloadUrl` (relative to image-generation.perchance.org).

If `waiting_for_prev_request_to_finish`:
`GET /api/awaitExistingGenerationRequest?userKey=` then retry POST.

Ad code: `GET https://perchance.org/api/getAccessCodeForAdPoweredStuff` → raw 64 hex.
Can be empty; generator.har generated with `adAccessCode=""`.
