"""test_farm_userkeys.py — Tests for the multi-proxy userKey harvester."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

# Windows UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import farm_userkeys  # noqa: E402


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestLoadProxies(unittest.TestCase):
    def test_loads_plain_text(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt",
                                         delete=False, encoding="utf-8") as f:
            f.write("1.2.3.4:8080\n5.6.7.8:3128\n# comment\n\n9.10.11.12:80\n")
            path = Path(f.name)
        try:
            ps = farm_userkeys.load_proxies(path)
            self.assertEqual(ps, ["1.2.3.4:8080", "5.6.7.8:3128", "9.10.11.12:80"])
        finally:
            path.unlink()

    def test_loads_json_list(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump(["1.2.3.4:8080", "5.6.7.8:3128"], f)
            path = Path(f.name)
        try:
            ps = farm_userkeys.load_proxies(path)
            self.assertEqual(ps, ["1.2.3.4:8080", "5.6.7.8:3128"])
        finally:
            path.unlink()

    def test_loads_json_with_proxy_field(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump([{"proxy": "1.2.3.4:8080"}, {"ip:port": "5.6.7.8:3128"}], f)
            path = Path(f.name)
        try:
            ps = farm_userkeys.load_proxies(path)
            self.assertEqual(ps, ["1.2.3.4:8080", "5.6.7.8:3128"])
        finally:
            path.unlink()

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            farm_userkeys.load_proxies(Path("/nonexistent/proxies.txt"))


class TestProxyUrl(unittest.TestCase):
    def test_passthrough(self):
        self.assertEqual(farm_userkeys.make_proxy_url("http://x.com:80"),
                         "http://x.com:80")
        self.assertEqual(farm_userkeys.make_proxy_url("socks5://x.com:1080"),
                         "socks5://x.com:1080")

    def test_adds_http_scheme(self):
        self.assertEqual(farm_userkeys.make_proxy_url("1.2.3.4:8080"),
                         "http://1.2.3.4:8080")
        self.assertEqual(farm_userkeys.make_proxy_url("user:pass@1.2.3.4:8080"),
                         "http://user:pass@1.2.3.4:8080")


class TestAttemptOneProxy(unittest.TestCase):
    def _make_sess(self, mock_responses: list):
        """Return a fake session whose .request() yields the next response."""
        sess = MagicMock()
        sess.headers = {}
        sess.proxies = {}
        iter_responses = iter(mock_responses)
        def _request(method, url, **kw):
            try:
                return next(iter_responses)
            except StopIteration:
                return None
        sess.request.side_effect = _request
        return sess

    def _mock_resp(self, status=200, body=""):
        r = MagicMock()
        r.status_code = status
        r.text = body
        r.content = body.encode()
        r.headers = {"content-type": "text/html"}
        r.cookies = []
        return r

    def test_full_success_path(self):
        """All 4 steps succeed → status='success' with userKey."""
        uk = "a" * 64
        ad = "b" * 64
        responses = [
            self._mock_resp(200, "<html><body>main</body></html>"),
            self._mock_resp(200, "<html><body>embed</body></html>"),
            self._mock_resp(200, ad),  # adCode
            self._mock_resp(200, f'{{"status":"verified","userKey":"{uk}"}}'),
        ]
        sess = self._make_sess(responses)
        with patch.object(farm_userkeys, "cffi_requests") as mod, \
             patch.object(farm_userkeys, "_CURL_CFFI_AVAILABLE", True):
            mod.Session.return_value = sess
            r = farm_userkeys.attempt_one_proxy("1.2.3.4:8080",
                                                log_prefix="")
            self.assertEqual(r["status"], "success")
            self.assertEqual(r["userKey"], uk)
            self.assertEqual(r["adCode"], ad)
            self.assertGreaterEqual(r["requests_made"], 4)
            # Proxy was set on the session
            self.assertEqual(sess.proxies["http"], "http://1.2.3.4:8080")
            self.assertEqual(sess.proxies["https"], "http://1.2.3.4:8080")

    def test_main_page_403(self):
        responses = [self._mock_resp(403, "Forbidden")]
        sess = self._make_sess(responses)
        with patch.object(farm_userkeys, "cffi_requests") as mod, \
             patch.object(farm_userkeys, "_CURL_CFFI_AVAILABLE", True):
            mod.Session.return_value = sess
            r = farm_userkeys.attempt_one_proxy("1.2.3.4:8080", log_prefix="")
            self.assertIn("main_page", r["status"])
            self.assertNotEqual(r["status"], "success")
            self.assertIsNone(r["userKey"])

    def test_verifyUser_token_required(self):
        """Server returns token_required on all 3 threads → no userKey."""
        responses = [
            self._mock_resp(200, "<html>main</html>"),
            self._mock_resp(200, "<html>embed</html>"),
            self._mock_resp(200, "b" * 64),  # adCode ok
            self._mock_resp(200, '{"status":"failed_verification","reason":"token_required"}'),
            self._mock_resp(200, '{"status":"failed_verification","reason":"token_required"}'),
            self._mock_resp(200, '{"status":"failed_verification","reason":"token_required"}'),
        ]
        sess = self._make_sess(responses)
        with patch.object(farm_userkeys, "cffi_requests") as mod, \
             patch.object(farm_userkeys, "_CURL_CFFI_AVAILABLE", True):
            mod.Session.return_value = sess
            r = farm_userkeys.attempt_one_proxy("1.2.3.4:8080", log_prefix="")
            self.assertEqual(r["status"], "verifyUser_no_userkey")
            self.assertIsNone(r["userKey"])
            self.assertEqual(r["adCode"], "b" * 64)  # adCode captured

    def test_picks_first_thread_with_userkey(self):
        """Thread 0 fails (token_required), thread 1 succeeds."""
        uk = "c" * 64
        responses = [
            self._mock_resp(200, "<html>main</html>"),
            self._mock_resp(200, "<html>embed</html>"),
            self._mock_resp(200, "b" * 64),
            self._mock_resp(200, '{"reason":"token_required"}'),  # thread 0
            self._mock_resp(200, f'{{"userKey":"{uk}"}}'),  # thread 1
        ]
        sess = self._make_sess(responses)
        with patch.object(farm_userkeys, "cffi_requests") as mod, \
             patch.object(farm_userkeys, "_CURL_CFFI_AVAILABLE", True):
            mod.Session.return_value = sess
            r = farm_userkeys.attempt_one_proxy("1.2.3.4:8080", log_prefix="")
            self.assertEqual(r["status"], "success")
            self.assertEqual(r["userKey"], uk)


class TestMain(unittest.TestCase):
    def test_no_curl_cffi_returns_1(self):
        with patch.object(farm_userkeys, "_CURL_CFFI_AVAILABLE", False):
            rc = farm_userkeys.main(["farm_userkeys.py"])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
