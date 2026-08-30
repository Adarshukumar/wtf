# Flow reconstructed from generator.har + prompt.har

## Mint (DrissionPage)

1. Chromium, optional `--proxy-server`.
2. `GET https://image-generation.perchance.org/embed#<url-encoded JSON>`
   Hash **must** include a non-empty `prompt` or embed never calls verifyUser.
3. Listen:
   - ignore `{"status":"failed_verification","reason":"token_required"}`
   - wait for Turnstile inside the page
   - accept `{"status":"success","userKey":…}` or `already_verified`
4. Close Chrome. Do not download the image from Chrome.

`userKey` is also written to `localStorage['userKey-'+thread]`.

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
