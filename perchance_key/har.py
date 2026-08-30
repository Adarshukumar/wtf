"""Read the captured Chrome HAR and reconstruct the live protocol.

The HAR (`perchance.org.json`) is a WebInspector 1.2 dump of
https://perchance.org/imageapi — response bodies for the image-generation
API were stripped by DevTools (size>0, text empty), but URLs, methods,
query strings, and the generate POST body are intact.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .parse import AD_CODE_QS_RE, USER_KEY_QS_RE


@dataclass
class HarFacts:
    page_url: str | None
    entry_count: int
    user_keys: list[str]
    ad_access_codes: list[str]
    generate_template: dict[str, Any] | None
    endpoints: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_url": self.page_url,
            "entry_count": self.entry_count,
            "user_keys": self.user_keys,
            "ad_access_codes": self.ad_access_codes,
            "generate_template": self.generate_template,
            "endpoints": self.endpoints,
            "notes": self.notes,
        }


FOCUS_HOSTS = {
    "perchance.org",
    "image-generation.perchance.org",
}


FOCUS_PATH_PARTS = (
    "/api/verifyUser",
    "/api/generate",
    "/api/getUserQueuePosition",
    "/api/awaitExistingGenerationRequest",
    "/api/downloadTemporaryImage",
    "/api/downloadTemporaryImageViaProxy",
    "/api/getAccessCodeForAdPoweredStuff",
    "/api/securityData",
    "/api/alc",
    "/embed",
    "/imageapi",
)


def load_har(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def analyze(path: Path) -> HarFacts:
    har = load_har(path)
    entries = har.get("log", {}).get("entries", [])
    pages = har.get("log", {}).get("pages", [])
    page_url = pages[0]["title"] if pages else None

    keys: list[str] = []
    ads: list[str] = []
    generate_template: dict[str, Any] | None = None
    endpoint_counter: Counter[tuple[str, str, str]] = Counter()

    for e in entries:
        req = e.get("request") or {}
        url = req.get("url") or ""
        parsed = urlparse(url)
        if parsed.netloc not in FOCUS_HOSTS and not parsed.netloc.endswith(".perchance.org"):
            continue
        if not any(part in parsed.path for part in FOCUS_PATH_PARTS):
            continue
        if "cdn-cgi" in parsed.path:
            continue
        method = req.get("method") or "?"
        endpoint_counter[(method, parsed.netloc, parsed.path)] += 1

        m = USER_KEY_QS_RE.search(url)
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
        m = AD_CODE_QS_RE.search(url)
        if m and m.group(1) not in ads:
            ads.append(m.group(1))

        if parsed.path.endswith("/api/generate") and generate_template is None:
            post = (req.get("postData") or {}).get("text") or ""
            try:
                body = json.loads(post) if post else {}
            except json.JSONDecodeError:
                body = {"raw": post}
            generate_template = {
                "method": "POST",
                "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                "query_keys": sorted(parse_qs(parsed.query).keys()),
                "content_type": (req.get("postData") or {}).get("mimeType"),
                "json_body": body,
                "headers": {
                    h["name"]: h["value"]
                    for h in req.get("headers") or []
                    if h.get("name", "").lower()
                    in {"content-type", "origin", "referer", "user-agent", "accept"}
                },
            }

    notes = [
        "userKey is issued by GET image-generation.perchance.org/api/verifyUser "
        "(embed origin). HAR bodies were stripped (~106 bytes JSON).",
        "adAccessCode is issued by GET perchance.org/api/getAccessCodeForAdPoweredStuff "
        "(plain 64-char hex).",
        "userKey is IP-sticky: generate / queue / download MUST reuse the same proxy "
        "that minted the key.",
        "Chrome never sent cookies on /api/generate — identity is the userKey + IP.",
        "Parent page loads hashed-subdomain iframe then many "
        "image-generation.perchance.org/embed iframes (one per image).",
        "Cloudflare sits in front (cf-ray, cdn-cgi/challenge-platform) — DrissionPage "
        "Chromium is required to mint a key; curl_cffi impersonation is for later API calls.",
    ]

    endpoints = [
        {
            "method": method,
            "host": host,
            "path": path,
            "count": count,
        }
        for (method, host, path), count in sorted(endpoint_counter.items(), key=lambda x: (-x[1], x[0][2]))
    ]

    return HarFacts(
        page_url=page_url,
        entry_count=len(entries),
        user_keys=keys,
        ad_access_codes=ads,
        generate_template=generate_template,
        endpoints=endpoints,
        notes=notes,
    )
