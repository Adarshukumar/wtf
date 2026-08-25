"""
test_perchance_v2.py — Tests for perchance_v2.py.
"""

from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))
import perchance     # noqa: E402
import perchance_v2  # noqa: E402


class TestFingerprintDiscovery(unittest.TestCase):
    def test_detect_supported_fingerprints(self):
        """Returns a list (possibly empty if curl_cffi not installed)."""
        fps = perchance_v2.detect_supported_fingerprints()
        self.assertIsInstance(fps, list)
        for fp in fps:
            self.assertIsInstance(fp, str)

    def test_curl_cffi_version(self):
        v = perchance_v2.detect_curl_cffi_version()
        self.assertTrue(v is None or isinstance(v, str))


class TestClientInit(unittest.TestCase):
    def test_init_with_learner(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(perchance, "POLICY_STATE_FILE", Path(tmp) / "p.json"):
                client = perchance_v2.PerchanceClientV2()
                self.assertIsNotNone(client.learner)
                self.assertIsNotNone(client.supported_fps)


class TestFailureClassification(unittest.TestCase):
    def test_cert_error_recognised(self):
        client = perchance_v2.PerchanceClientV2()
        exc = Exception("SSL certificate problem: unable to get local issuer certificate")
        reward, blocked, errored, success, detail = client._classify_failure(exc, None)
        self.assertEqual(reward, perchance_v2.REWARD_CERT_ERROR)
        self.assertTrue(blocked)

    def test_not_supported_recognised(self):
        client = perchance_v2.PerchanceClientV2()
        exc = Exception("Impersonating chrome146 is not supported")
        reward, blocked, errored, success, detail = client._classify_failure(exc, None)
        self.assertEqual(reward, perchance_v2.REWARD_NOT_SUPPORTED)
        self.assertTrue(blocked)

    def test_connection_closed_recognised(self):
        client = perchance_v2.PerchanceClientV2()
        exc = Exception("Connection closed abruptly")
        reward, blocked, errored, success, detail = client._classify_failure(exc, None)
        self.assertEqual(reward, perchance_v2.REWARD_CONN_CLOSED)
        self.assertTrue(blocked)

    def test_generic_error(self):
        client = perchance_v2.PerchanceClientV2()
        exc = Exception("something else")
        reward, blocked, errored, success, detail = client._classify_failure(exc, None)
        self.assertEqual(reward, perchance.REWARD_NETWORK_ERR)
        self.assertTrue(errored)

    def test_http_classify_200(self):
        client = perchance_v2.PerchanceClientV2()
        class _R:
            status_code = 200
            text = "ok"
        reward, blocked, errored, success, detail = client._http_classify(_R())
        self.assertEqual(success, True)
        self.assertEqual(blocked, False)

    def test_http_classify_403(self):
        client = perchance_v2.PerchanceClientV2()
        class _R:
            status_code = 403
            text = ""
        reward, blocked, errored, success, detail = client._http_classify(_R())
        self.assertEqual(reward, perchance.REWARD_FORBIDDEN)
        self.assertTrue(blocked)

    def test_http_classify_429(self):
        client = perchance_v2.PerchanceClientV2()
        class _R:
            status_code = 429
            text = ""
        reward, blocked, errored, success, detail = client._http_classify(_R())
        self.assertEqual(reward, perchance.REWARD_RATE_LIMITED)
        self.assertTrue(blocked)

    def test_http_classify_cloudflare(self):
        client = perchance_v2.PerchanceClientV2()
        for sc in (520, 521, 522, 523, 524, 525, 526, 527, 530):
            class _R:
                status_code = sc
                text = ""
            reward, blocked, _, _, _ = client._http_classify(_R())
            self.assertEqual(reward, perchance.REWARD_BLOCKED)


class TestUserKeyCache(unittest.TestCase):
    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(perchance_v2, "USERKEY_CACHE_FILE", Path(tmp) / "k.json"):
                client = perchance_v2.PerchanceClientV2()
                self.assertIsNone(client._load_cached_user_key())
                client._save_user_key("a" * 64)
                self.assertEqual(client._load_cached_user_key(), "a" * 64)


class TestVerifyUserFlow(unittest.TestCase):
    def test_unsupported_fingerprint_returns_blocked(self):
        client = perchance_v2.PerchanceClientV2()
        bad_arm = None
        for a in client.learner.arms:
            if a.fingerprint not in client.supported_fps:
                bad_arm = a
                break
        if bad_arm is None:
            self.skipTest("all fingerprints in your build are supported")
        res = client.verify_user(bad_arm)
        self.assertFalse(res.ok)
        self.assertTrue(res.blocked)
        # The message should mention either 'not supported' or 'not in your'
        self.assertTrue(
            "not supported" in res.detail or "not in your" in res.detail,
            f"unexpected detail: {res.detail!r}",
        )

    def test_successful_verifyuser(self):
        client = perchance_v2.PerchanceClientV2()
        good_arm = None
        for a in client.learner.arms:
            if a.fingerprint in client.supported_fps:
                good_arm = a
                break
        if good_arm is None:
            self.skipTest("no supported fingerprints in this build")

        body = json.dumps({"status": "already_verified", "userKey": "a" * 64})

        class _FakeResp:
            status_code = 200
            text = body
            headers = {"content-type": "application/json"}
            def json(self):
                return json.loads(self.text)

        with patch.object(client, "_make_session") as ms:
            sess = MagicMock()
            sess.get.return_value = _FakeResp()
            ms.return_value = sess
            res = client.verify_user(good_arm, with_page_warmup=False)
            self.assertTrue(res.ok)
            self.assertEqual(res.user_key, "a" * 64)
            self.assertEqual(res.reward, perchance.REWARD_GOT_USERKEY)

    def test_blocked_403(self):
        client = perchance_v2.PerchanceClientV2()
        good_arm = next(
            (a for a in client.learner.arms
             if a.fingerprint in client.supported_fps),
            None,
        )
        if good_arm is None:
            self.skipTest("no supported fingerprints")

        class _FakeResp:
            status_code = 403
            text = "blocked"
            headers = {}

        with patch.object(client, "_make_session") as ms:
            sess = MagicMock()
            sess.get.return_value = _FakeResp()
            ms.return_value = sess
            res = client.verify_user(good_arm)
            self.assertFalse(res.ok)
            self.assertTrue(res.blocked)


class TestAcquireUserKeyFallback(unittest.TestCase):
    def test_acquire_returns_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(perchance_v2, "USERKEY_CACHE_FILE", Path(tmp) / "k.json"):
                client = perchance_v2.PerchanceClientV2()
                client._save_user_key("b" * 64)
                res = client.acquire_user_key(verbose=False)
                self.assertTrue(res.ok)
                self.assertEqual(res.user_key, "b" * 64)
                self.assertIn("cache", res.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
