"""Replay generator.har + prompt.har — the real protocol, not guesses."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from perchance.parse import parse_ad_access, parse_check_status, parse_verify_user

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "generator.har"
PROMPT = ROOT / "prompt.har"

KEY = "2c9aff54b883dadeff830a2819af9fb42c38a368ad020bdc5046ec0e93cb8b04"
AD = "344aafad2512af44857b865746acda1128141b2b6a3ae5b68a440c26fb1ea6c2"


def _entries(path: Path) -> list[dict]:
    return json.loads(path.read_text())["log"]["entries"]


def _text(e: dict) -> str:
    return (e.get("response") or {}).get("content", {}).get("text") or ""


def test_files_exist():
    assert GEN.exists() and PROMPT.exists()


def test_verify_user_sequence_in_generator_har():
    bodies = []
    for e in _entries(GEN):
        url = e["request"]["url"]
        if "/api/verifyUser" in url:
            bodies.append((_text(e), "token=" in url))

    statuses = [json.loads(t)["status"] for t, _ in bodies]
    counts = Counter(statuses)
    assert counts["failed_verification"] == 15
    assert counts["success"] == 1
    assert counts["already_verified"] == 2

    # tokenless calls fail until Turnstile; the success call carries ?token=
    first_success = next(i for i, (t, _) in enumerate(bodies) if json.loads(t)["status"] == "success")
    assert bodies[first_success][1] is True
    for t, had_token in bodies[:first_success]:
        assert json.loads(t)["status"] == "failed_verification"
        assert json.loads(t)["reason"] == "token_required"
        assert had_token is False

    parsed = parse_verify_user(bodies[first_success][0])
    assert parsed == {"verified": True, "status": "success", "user_key": KEY, "reason": None}

    pending = parse_verify_user(bodies[0][0])
    assert pending["verified"] is False
    assert pending["reason"] == "token_required"


def test_already_verified_parser():
    body = json.dumps({"status": "already_verified", "userKey": KEY})
    assert parse_verify_user(body)["user_key"] == KEY
    assert parse_verify_user(body)["verified"] is True


def test_generate_success_shape_generator_har():
    successes = []
    for e in _entries(GEN):
        if "/api/generate" not in e["request"]["url"]:
            continue
        t = _text(e)
        if not t:
            continue
        data = json.loads(t)
        if data.get("status") == "success":
            successes.append((json.loads(e["request"]["postData"]["text"]), data))
    assert successes
    body, resp = successes[0]
    assert body["userKey"] == KEY
    assert body["channel"] == "ai-text-to-image-generator"
    assert body["resolution"] == "768x768"
    assert body["adAccessCode"] == ""
    assert resp["imageId"]
    assert resp["imageDownloadUrl"].startswith("/api/downloadTemporaryImageViaProxy?t=")
    assert resp["fileExtension"] == "jpeg"


def test_prompt_har_uses_ad_access_and_imageapi_channel():
    ads = []
    channels = set()
    for e in _entries(PROMPT):
        url = e["request"]["url"]
        if "getAccessCodeForAdPoweredStuff" in url:
            ads.append(parse_ad_access(_text(e)))
        if "/api/generate" in url and e["request"].get("postData"):
            b = json.loads(e["request"]["postData"]["text"])
            channels.add(b["channel"])
            assert b["userKey"] == KEY
            assert b["adAccessCode"] == AD
    assert AD in ads
    assert channels == {"imageapi"}


def test_check_status_size_matches_verified_json():
    # prompt.har body was stripped; size 21 == len('{"status":"verified"}')
    found = False
    for e in _entries(PROMPT):
        if "checkUserVerificationStatus" in e["request"]["url"]:
            assert e["response"]["content"]["size"] == 21
            assert KEY in e["request"]["url"]
            found = True
    assert found
    assert parse_check_status('{"status":"verified"}') == "verified"


def test_waiting_status():
    seen = False
    for e in _entries(GEN):
        t = _text(e)
        if "waiting_for_prev_request_to_finish" in t:
            seen = True
            break
    assert seen
