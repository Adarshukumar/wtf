"""
test_deep_logger.py — Tests for deep_logger.py.
"""

from __future__ import annotations
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))
import deep_logger  # noqa: E402


class TestNetEvent(unittest.TestCase):
    def test_to_dict(self):
        ev = deep_logger.NetEvent(seq=1, kind="info", note="hello")
        d = ev.to_dict()
        self.assertEqual(d["seq"], 1)
        self.assertEqual(d["kind"], "info")
        self.assertEqual(d["note"], "hello")

    def test_defaults(self):
        ev = deep_logger.NetEvent()
        self.assertEqual(ev.seq, 0)
        self.assertEqual(ev.kind, "")
        self.assertEqual(ev.request_headers, {})


class TestEventLog(unittest.TestCase):
    def test_emit_appends(self):
        log = deep_logger.EventLog(console=False)
        log.info("a")
        log.info("b")
        self.assertEqual(len(log.events), 2)
        self.assertEqual([e.note for e in log.events], ["a", "b"])

    def test_seq_auto_increments(self):
        log = deep_logger.EventLog(console=False)
        log.info("a")
        log.info("b")
        log.info("c")
        self.assertEqual([e.seq for e in log.events], [1, 2, 3])

    def test_thread_safe(self):
        log = deep_logger.EventLog(console=False)

        def add():
            for _ in range(50):
                log.info("x")

        threads = [threading.Thread(target=add) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(log.events), 250)
        # All seqs should be unique
        seqs = [e.seq for e in log.events]
        self.assertEqual(len(set(seqs)), 250)

    def test_shorthand_methods(self):
        log = deep_logger.EventLog(console=False)
        log.request("GET", "https://example.com")
        log.response("GET", "https://example.com", 200, 100, 12.5,
                     {"content-type": "text/html"}, "OK")
        log.net_error("GET", "https://example.com", OSError("nope"), 5.0)
        log.redirect(302, "https://a.com", "https://b.com")
        log.cookie("sid", "abc123", "example.com", "/")
        kinds = [e.kind for e in log.events]
        self.assertEqual(kinds, ["request", "response", "error",
                                  "redirect", "cookie"])

    def test_html_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "log.html"
            log = deep_logger.EventLog(html_path=out, console=False)
            log.info("hello")
            log.request("GET", "https://example.com")
            log.write_html()
            self.assertTrue(out.exists())
            content = out.read_text()
            self.assertIn("deep_logger.py", content)
            self.assertIn("hello", content)
            self.assertIn("REQUEST", content)
            self.assertIn("https://example.com", content)

    def test_html_export_with_response_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "log.html"
            log = deep_logger.EventLog(html_path=out, console=False)
            log.response("GET", "https://example.com", 200, 100, 12.5,
                         {"content-type": "text/html",
                          "set-cookie": "sid=abc123"},
                         "<html>body</html>")
            log.write_html()
            content = out.read_text()
            self.assertIn("Response headers", content)
            self.assertIn("Body preview", content)
            self.assertIn("set-cookie", content)
            self.assertIn("text/html", content)


class TestFingerprintDetection(unittest.TestCase):
    def test_returns_string(self):
        fp = deep_logger.detect_best_fingerprint()
        self.assertIsInstance(fp, str)


class TestResourceExtraction(unittest.TestCase):
    def test_extracts_scripts(self):
        html = '''
        <html><head>
            <script src="https://x.com/a.js"></script>
            <script src="https://x.com/a.js"></script>
            <script data-src="https://x.com/b.js"></script>
        </head></html>
        '''
        res = deep_logger.extract_resources(html)
        self.assertEqual(len(res["scripts"]), 2)

    def test_extracts_all_kinds(self):
        html = '''
        <html>
            <head>
                <link rel="stylesheet" href="x.css">
                <link rel="prefetch" href="pre.js" as="script">
                <link rel="preconnect" href="https://cdn.x.com">
                <script src="app.js"></script>
            </head>
            <body>
                <img src="logo.png">
                <iframe src="https://embed.x.com/"></iframe>
            </body>
        </html>
        '''
        res = deep_logger.extract_resources(html)
        self.assertIn("x.css", res["links"])
        self.assertIn("pre.js", res["prefetch"])
        self.assertIn("https://cdn.x.com", res["preconnect"])
        self.assertIn("app.js", res["scripts"])
        self.assertIn("logo.png", res["images"])
        self.assertIn("https://embed.x.com/", res["iframes"])

    def test_empty_html(self):
        res = deep_logger.extract_resources("")
        for k in ("scripts", "links", "images", "iframes",
                  "prefetch", "preconnect"):
            self.assertEqual(res[k], [])


class TestFormatting(unittest.TestCase):
    def test_short(self):
        self.assertEqual(deep_logger._short("hi"), "hi")
        self.assertEqual(deep_logger._short("a" * 200, 50), "a" * 49 + "…")

    def test_fmt_size(self):
        self.assertEqual(deep_logger._fmt_size(100), "100B")
        self.assertEqual(deep_logger._fmt_size(2048), "2.0KB")
        self.assertEqual(deep_logger._fmt_size(1024 * 1024 * 5), "5.0MB")


class TestHtmlRefresher(unittest.TestCase):
    def test_refresher_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "live.html"
            log = deep_logger.EventLog(html_path=out, console=False)
            log.info("init")
            stop = threading.Event()
            t = threading.Thread(target=deep_logger._html_refresher,
                                 args=(log, out, stop), daemon=True)
            t.start()
            log.info("update1")
            time.sleep(1.5)
            log.info("update2")
            time.sleep(1.5)
            stop.set()
            t.join(timeout=2)
            self.assertTrue(out.exists())
            content = out.read_text()
            # The file should have at least the later events
            self.assertIn("update2", content)


class TestRunWithMock(unittest.TestCase):
    def test_deep_driver_runs_phases(self):
        """Drive the DeepDriver with mocked network, verify sub-resource
        extraction works and the script is fetched."""

        class _FakeResp:
            def __init__(self, status=200, body="<html></html>",
                         ctype="text/html", is_redirect=False, cookies=None):
                self.status_code = status
                self.text = body
                self.content = body.encode()
                self.headers = {"content-type": ctype}
                self.is_redirect = is_redirect
                self.cookies = cookies or []

        class _FakeSess:
            def __init__(self):
                self.headers = {}
                self.verify = True
            def request(self, method, url, **kw):
                if "imageapi" in url and "prompt=" in url:
                    return _FakeResp(
                        body='<html><head>'
                             '<script src="https://x.com/app.js"></script>'
                             '</head></html>')
                if "app.js" in url:
                    return _FakeResp(body="// js code",
                                     ctype="application/javascript")
                return _FakeResp(body="OK")

        fake_sess = _FakeSess()

        with patch.object(deep_logger, "cffi_requests") as mod, \
             patch.object(deep_logger, "_CURL_CFFI_AVAILABLE", True):
            mod.RequestException = Exception
            class _MM(dict):
                def keys(self): return ["chrome131"]
            mod.BrowserType = MagicMock()
            mod.BrowserType.__members__ = _MM()
            mod.Session.return_value = fake_sess

            log = deep_logger.EventLog(console=False)
            driver = deep_logger.DeepDriver(log)
            driver.run("https://perchance.org/imageapi?prompt=a%20cute%20booy",
                       with_iframe=False, with_api=False)

            kinds = [e.kind for e in log.events]
            self.assertIn("info", kinds)
            self.assertIn("request", kinds)
            self.assertIn("response", kinds)
            urls = [e.url for e in log.events if e.url]
            # Main page + the script extracted from it
            self.assertTrue(any("app.js" in u for u in urls),
                            f"app.js not in: {urls[:5]}")
            self.assertTrue(any("imageapi" in u for u in urls))

    def test_iframe_phase_uses_userkey(self):
        """Verify the iframe replay makes a verifyUser call and gets a userKey."""

        class _FakeResp:
            def __init__(self, status=200, body="<html></html>",
                         ctype="text/html", is_redirect=False, cookies=None):
                self.status_code = status
                self.text = body
                self.content = body.encode()
                self.headers = {"content-type": ctype}
                self.is_redirect = is_redirect
                self.cookies = cookies or []

        # Note: NO script or iframe in the main page body so phase 2 is empty,
        # but the iframe phase 4 still runs and should hit verifyUser
        class _FakeSess:
            def __init__(self):
                self.headers = {}
                self.verify = True
            def request(self, method, url, **kw):
                if "imageapi" in url and "prompt=" in url:
                    return _FakeResp(body="<html><head></head></html>")
                if "verifyUser" in url:
                    return _FakeResp(body='{"status":"verified","userKey":"' + "a" * 64 + '"}')
                if "embed" in url:
                    return _FakeResp(body="<html>embed</html>")
                return _FakeResp(body="OK")

        with patch.object(deep_logger, "cffi_requests") as mod, \
             patch.object(deep_logger, "_CURL_CFFI_AVAILABLE", True):
            mod.RequestException = Exception
            class _MM(dict):
                def keys(self): return ["chrome131"]
            mod.BrowserType = MagicMock()
            mod.BrowserType.__members__ = _MM()
            mod.Session.return_value = _FakeSess()

            log = deep_logger.EventLog(console=False)
            driver = deep_logger.DeepDriver(log)
            driver.run("https://perchance.org/imageapi?prompt=a%20cute%20booy",
                       with_iframe=True, with_api=False)

            urls = [e.url for e in log.events if e.url]
            self.assertTrue(any("verifyUser" in u for u in urls),
                            f"verifyUser not in: {urls[:5]}")
            # userKey should appear in the body of the verifyUser response
            userkey_responses = [
                e for e in log.events
                if e.kind == "response" and "verifyUser" in (e.url or "")
            ]
            self.assertTrue(len(userkey_responses) > 0)
            self.assertIn("userKey", userkey_responses[0].body_preview)


if __name__ == "__main__":
    unittest.main(verbosity=2)
