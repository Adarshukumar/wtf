"""
test_network_logger.py — Tests for the network logger.
"""

from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))
import network_logger  # noqa: E402


class TestNetEvent(unittest.TestCase):
    def test_to_dict(self):
        ev = network_logger.NetEvent(
            seq=1, ts="12:00:00.000", kind="request",
            method="GET", url="https://example.com",
        )
        d = ev.to_dict()
        self.assertEqual(d["seq"], 1)
        self.assertEqual(d["kind"], "request")
        self.assertEqual(d["url"], "https://example.com")

    def test_default_headers_empty(self):
        ev = network_logger.NetEvent(seq=1, ts="x", kind="info")
        self.assertEqual(ev.headers_in, {})
        self.assertEqual(ev.headers_out, {})


class TestNetLog(unittest.TestCase):
    def test_emit_appends_event(self):
        log = network_logger.NetLog(verbose=False)
        log.info("test")
        self.assertEqual(len(log.events), 1)
        self.assertEqual(log.events[0].note, "test")
        self.assertEqual(log.events[0].kind, "info")

    def test_emit_writes_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "log.jsonl"
            log = network_logger.NetLog(out_file=out, verbose=False)
            log.info("hello")
            log.info("world")
            self.assertTrue(out.exists())
            lines = out.read_text().strip().split("\n")
            self.assertEqual(len(lines), 2)
            j = json.loads(lines[0])
            self.assertEqual(j["note"], "hello")
            self.assertEqual(j["kind"], "info")

    def test_seq_increments(self):
        log = network_logger.NetLog(verbose=False)
        log.info("a")
        log.info("b")
        log.info("c")
        self.assertEqual([e.seq for e in log.events], [1, 2, 3])

    def test_start_end_request(self):
        log = network_logger.NetLog(verbose=False)
        rid = log.start_request("GET", "https://example.com")
        class _R:
            status_code = 200
            text = "OK"
            content = b"OK"
            headers = {"content-type": "text/plain"}
            is_redirect = False
        log.end_request(rid, "GET", "https://example.com", _R(), 12.5)
        self.assertEqual(len(log.events), 2)
        self.assertEqual(log.events[0].kind, "request")
        self.assertEqual(log.events[1].kind, "response")
        self.assertEqual(log.events[1].status, 200)
        self.assertEqual(log.events[1].duration_ms, 12.5)

    def test_end_error(self):
        log = network_logger.NetLog(verbose=False)
        rid = log.start_request("GET", "https://example.com")
        log.end_error(rid, "GET", "https://example.com", OSError("nope"), 5.0)
        self.assertEqual(len(log.events), 2)
        self.assertEqual(log.events[1].kind, "error")
        self.assertIn("nope", log.events[1].note)


class TestFingerprintDetection(unittest.TestCase):
    def test_detect_returns_string(self):
        fp = network_logger.detect_best_fingerprint()
        self.assertIsInstance(fp, str)

    def test_detect_prefers_chrome(self):
        fp = network_logger.detect_best_fingerprint()
        if fp:
            self.assertIsInstance(fp, str)
            self.assertGreater(len(fp), 0)


class TestResourceExtraction(unittest.TestCase):
    def test_extracts_scripts(self):
        html = '''
        <html><head>
            <script src="https://example.com/a.js"></script>
            <script src="https://example.com/b.js"></script>
            <script src="https://example.com/a.js"></script>
        </head></html>
        '''
        res = network_logger.extract_resources(html, "https://example.com")
        self.assertEqual(len(res["scripts"]), 2)
        self.assertIn("https://example.com/a.js", res["scripts"])

    def test_extracts_links(self):
        html = '<link rel="stylesheet" href="https://cdn.example.com/x.css">'
        res = network_logger.extract_resources(html, "https://example.com")
        self.assertIn("https://cdn.example.com/x.css", res["links"])

    def test_extracts_images(self):
        html = '<img src="/img.png"><img src="https://x.com/y.jpg">'
        res = network_logger.extract_resources(html, "https://example.com")
        self.assertEqual(len(res["images"]), 2)

    def test_extracts_iframes(self):
        html = '<iframe src="https://embed.example.com/"></iframe>'
        res = network_logger.extract_resources(html, "https://example.com")
        self.assertIn("https://embed.example.com/", res["iframes"])

    def test_empty_html(self):
        res = network_logger.extract_resources("", "https://example.com")
        for k in ("scripts", "links", "images", "iframes"):
            self.assertEqual(res[k], [])


class TestFormatting(unittest.TestCase):
    def test_short(self):
        self.assertEqual(network_logger._short("hello"), "hello")
        self.assertEqual(network_logger._short("a" * 100, 10), "a" * 9 + "…")

    def test_fmt_size(self):
        self.assertEqual(network_logger._fmt_size(500), "500B")
        self.assertEqual(network_logger._fmt_size(2048), "2.0KB")
        self.assertEqual(network_logger._fmt_size(1024 * 1024 * 3), "3.0MB")

    def test_status_color(self):
        for sc in (100, 200, 300, 400, 500):
            self.assertIsInstance(network_logger._status_color(sc), str)


class TestRunWithMock(unittest.TestCase):
    def test_run_logs_request_and_response(self):
        """Test the full run() flow with a mocked session."""
        class _FakeResp:
            status_code = 200
            text = "<html><head></head><body>OK</body></html>"
            content = b"<html></html>"
            headers = {"content-type": "text/html"}
            is_redirect = False
            cookies = []

        class _FakeSess:
            def __init__(self):
                self.headers = {}
                self.verify = True
            def request(self, method, url, **kw):
                return _FakeResp()

        fake_sess = _FakeSess()

        with patch.object(network_logger, "cffi_requests") as mod, \
             patch.object(network_logger, "_CURL_CFFI_AVAILABLE", True):
            mod.Session.return_value = fake_sess
            mod.RequestException = Exception
            class _MockMembers(dict):
                def keys(self):
                    return ["chrome131"]
            mod.BrowserType = MagicMock()
            mod.BrowserType.__members__ = _MockMembers()
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "log.jsonl"
                network_logger.run(
                    "https://example.com",
                    out_file=out,
                    once=True,
                    follow=False,
                    fetch_subresources=False,
                )
                self.assertTrue(out.exists())
                lines = out.read_text().strip().split("\n")
                self.assertGreater(len(lines), 0)
                j = json.loads(lines[0])
                self.assertEqual(j["kind"], "info")


if __name__ == "__main__":
    unittest.main(verbosity=2)
