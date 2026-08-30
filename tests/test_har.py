from pathlib import Path

from perchance_key.har import analyze

HAR = Path(__file__).resolve().parent.parent / "perchance.org.json"

KEY = "fe14a03dabcc4c3013f7f56db38bc7cda58b0da258fcef96b1c10afe08d5aa7e"
AD = "bdb81e510ad8273f27163560c1280622ecd6df7eabe87a7a8d4a94411812a843"


def test_har_present():
    assert HAR.exists(), "perchance.org.json must sit at repo root"


def test_har_protocol():
    facts = analyze(HAR)
    assert facts.entry_count == 1162
    assert facts.user_keys == [KEY]
    assert facts.ad_access_codes == [AD]
    assert facts.generate_template is not None
    body = facts.generate_template["json_body"]
    assert body["channel"] == "imageapi"
    assert body["subChannel"] == "public"
    assert body["userKey"] == KEY
    assert body["adAccessCode"] == AD
    assert body["prompt"] == "a cute booy"
    assert body["resolution"] == "512x768"
    assert facts.generate_template["content_type"].startswith("text/plain")
    paths = {(e["method"], e["path"]) for e in facts.endpoints}
    assert ("GET", "/api/verifyUser") in paths
    assert ("POST", "/api/generate") in paths
    assert ("GET", "/api/getUserQueuePosition") in paths
    assert ("GET", "/api/awaitExistingGenerationRequest") in paths
    assert ("GET", "/api/getAccessCodeForAdPoweredStuff") in paths
    assert ("GET", "/embed") in paths
