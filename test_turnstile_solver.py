"""test_turnstile_solver.py — Tests for the EzSolver client wrapper."""
from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

# UTF-8 on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import turnstile_solver  # noqa: E402


def _read(p):
    return p.read_text(encoding="utf-8")


class TestFindSitekey(unittest.TestCase):
    def test_finds_data_sitekey_attribute(self):
        html = '<div class="cf-turnstile" data-sitekey="0x4AAABBBCCC12345"></div>'
        self.assertEqual(turnstile_solver.find_sitekey(html), "0x4AAABBBCCC12345")

    def test_finds_inline_js_sitekey(self):
        html = """<script>turnstile.render('#foo', {sitekey: '0x4AAADDD'});</script>"""
        self.assertEqual(turnstile_solver.find_sitekey(html), "0x4AAADDD")

    def test_finds_url_param(self):
        html = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?sitekey=0x4AAAAAACCCC12345"></script>'
        self.assertEqual(turnstile_solver.find_sitekey(html), "0x4AAAAAACCCC12345")

    def test_finds_quoted_string(self):
        html = '<script>window.turnstile.render("#x", "0x4AAAAAACCCC98765")</script>'
        self.assertEqual(turnstile_solver.find_sitekey(html), "0x4AAAAAACCCC98765")

    def test_returns_none_if_no_sitekey(self):
        html = '<html><body>no turnstile here</body></html>'
        self.assertIsNone(turnstile_solver.find_sitekey(html))

    def test_handles_empty_string(self):
        self.assertIsNone(turnstile_solver.find_sitekey(""))


class TestIsSolverAvailable(unittest.TestCase):
    def test_returns_true_when_health_ok(self):
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"status": "ok"}).encode()
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = lambda s, *a: None
        with patch("urllib.request.urlopen", return_value=fake_response):
            self.assertTrue(turnstile_solver.is_solver_available())

    def test_returns_false_when_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            self.assertFalse(turnstile_solver.is_solver_available())


class TestSolve(unittest.TestCase):
    def test_returns_token_on_success(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({
            "token": "0.abc123", "elapsed": 4.2
        }).encode()
        fake.__enter__ = lambda s: s
        fake.__exit__ = lambda s, *a: None
        with patch("urllib.request.urlopen", return_value=fake):
            token, elapsed = turnstile_solver.solve("0xKEY", "https://x.com")
            self.assertEqual(token, "0.abc123")
            self.assertEqual(elapsed, 4.2)

    def test_raises_on_error_in_response(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"error": "Turnstile timeout"}).encode()
        fake.__enter__ = lambda s: s
        fake.__exit__ = lambda s, *a: None
        with patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(RuntimeError) as cm:
                turnstile_solver.solve("0xKEY", "https://x.com")
            self.assertIn("Turnstile timeout", str(cm.exception))

    def test_raises_on_unreachable(self):
        with patch("urllib.request.urlopen",
                   side_effect=Exception("Connection refused")):
            with self.assertRaises(RuntimeError):
                turnstile_solver.solve("0xKEY", "https://x.com")


class TestSolveWithRetry(unittest.TestCase):
    def test_returns_token_on_first_try(self):
        with patch.object(turnstile_solver, "solve",
                          return_value=("0.token", 1.0)) as mock_solve:
            t = turnstile_solver.solve_with_retry("0xK", "https://x.com")
            self.assertEqual(t, "0.token")
            self.assertEqual(mock_solve.call_count, 1)

    def test_retries_on_failure(self):
        with patch.object(turnstile_solver, "solve",
                          side_effect=[RuntimeError("fail"), ("0.token", 1.0)]) as mock:
            t = turnstile_solver.solve_with_retry("0xK", "https://x.com",
                                                  max_attempts=3)
            self.assertEqual(t, "0.token")
            self.assertEqual(mock.call_count, 2)

    def test_returns_none_after_max_attempts(self):
        with patch.object(turnstile_solver, "solve",
                          side_effect=RuntimeError("nope")):
            t = turnstile_solver.solve_with_retry("0xK", "https://x.com",
                                                  max_attempts=2)
            self.assertIsNone(t)


class TestFetchEmbedHtmlAndSitekey(unittest.TestCase):
    def test_extracts_sitekey_from_embed_page(self):
        sess = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '<html><div data-sitekey="0x4AAAAACAFEBABE99"></div></html>'
        sess.get.return_value = resp
        sk = turnstile_solver.fetch_embed_html_and_sitekey(sess, "https://x.com/embed")
        self.assertEqual(sk, "0x4AAAAACAFEBABE99")
        sess.get.assert_called_once()

    def test_returns_none_on_non_200(self):
        sess = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        sess.get.return_value = resp
        sk = turnstile_solver.fetch_embed_html_and_sitekey(sess, "https://x.com/embed")
        self.assertIsNone(sk)

    def test_returns_none_on_exception(self):
        sess = MagicMock()
        sess.get.side_effect = Exception("network down")
        sk = turnstile_solver.fetch_embed_html_and_sitekey(sess, "https://x.com/embed")
        self.assertIsNone(sk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
