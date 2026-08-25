"""
test_multi_proxy.py — Tests for multi_proxy.py (no network required).

Verifies:
  - extract_proxies() returns 50 distinct, valid proxies from the real miner DB
  - write_proxies_txt() persists them in the correct format
  - The FarmSimulator converges: with simulated ground truth, the bandit
    learns to prefer the high-reward arms
  - Per-user state persistence works
"""

from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import multi_proxy  # noqa: E402
import perchance    # noqa: E402


class TestProxyExtraction(unittest.TestCase):
    def setUp(self):
        self.db = multi_proxy.find_proxy_miner_db()
        if self.db is None:
            self.skipTest("proxy-miner DB not found")

    def test_extract_50_proxies(self):
        proxies = multi_proxy.extract_proxies(self.db, count=50, seed=42)
        self.assertEqual(len(proxies), 50)

    def test_proxies_are_unique(self):
        proxies = multi_proxy.extract_proxies(self.db, count=50, seed=42)
        addrs = [p.proxy for p in proxies]
        self.assertEqual(len(addrs), len(set(addrs)))

    def test_proxies_have_valid_format(self):
        import re
        proxies = multi_proxy.extract_proxies(self.db, count=20, seed=1)
        pat = re.compile(r"^\d+\.\d+\.\d+\.\d+:\d+$")
        for p in proxies:
            self.assertRegex(p.proxy, pat)
            self.assertGreater(p.port, 0)
            self.assertLess(p.port, 65536)
            self.assertTrue(p.protocols)  # every proxy has at least one protocol

    def test_filter_by_protocol(self):
        proxies = multi_proxy.extract_proxies(
            self.db, count=10, protocol="SOCKS5", seed=1,
        )
        for p in proxies:
            self.assertIn("SOCKS5", p.protocols)

    def test_filter_by_country(self):
        proxies = multi_proxy.extract_proxies(
            self.db, count=10, country_code="US", seed=1,
        )
        for p in proxies:
            self.assertEqual(p.country_code, "US")

    def test_diversification(self):
        """50 extracted proxies should span >1 country and >1 protocol."""
        proxies = multi_proxy.extract_proxies(self.db, count=50, seed=1)
        countries = {p.country_code for p in proxies}
        protocols = set()
        for p in proxies:
            protocols.update(p.protocols)
        self.assertGreater(len(countries), 1,
                           f"only 1 country in 50 proxies: {countries}")
        self.assertGreater(len(protocols), 1,
                           f"only 1 protocol in 50 proxies: {protocols}")

    def test_seed_reproducibility(self):
        a = multi_proxy.extract_proxies(self.db, count=30, seed=123)
        b = multi_proxy.extract_proxies(self.db, count=30, seed=123)
        self.assertEqual([p.proxy for p in a], [p.proxy for p in b])

    def test_different_seeds_yield_different_results(self):
        a = multi_proxy.extract_proxies(self.db, count=30, seed=1)
        b = multi_proxy.extract_proxies(self.db, count=30, seed=999)
        self.assertNotEqual([p.proxy for p in a], [p.proxy for p in b])

    def test_write_proxies_txt(self):
        proxies = multi_proxy.extract_proxies(self.db, count=10, seed=1)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "proxies.txt"
            multi_proxy.write_proxies_txt(proxies, out)
            self.assertTrue(out.exists())
            lines = out.read_text().splitlines()
            self.assertEqual(len(lines), 10)
            self.assertEqual(lines[0], proxies[0].proxy)


class TestBanditSimulation(unittest.TestCase):
    def test_simulator_runs(self):
        sim = multi_proxy.FarmSimulator(num_users=20, seed=42)
        res = sim.simulate(attempts_per_user=5)
        self.assertIn("acquired", res)
        self.assertEqual(res["num_users"], 20)

    def test_bandit_prefers_good_arms(self):
        """After enough training, the bandit should rank good arms highest."""
        # Reset state for a clean test
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(perchance, "POLICY_STATE_FILE", Path(tmp) / "policy.json"):
                sim = multi_proxy.FarmSimulator(num_users=200, seed=42)
                sim.simulate(attempts_per_user=10)
                top = sim.bandit.top_arms(3)
                # The simulator encodes a ground truth where chrome146+chrome-macos
                # is the best. The bandit should rank it #1.
                best = top[0]
                self.assertEqual(best.fingerprint, "chrome146")
                self.assertEqual(best.profile_name, "chrome-macos")
                self.assertGreater(best.success_rate, 0.5)


class TestUserSlot(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(multi_proxy, "USERKEYS_DIR", Path(tmp)):
                p = multi_proxy.Proxy(
                    ip="1.2.3.4", port=8080, proxy="1.2.3.4:8080",
                    country_code="US", protocols=["HTTP"],
                )
                arm = perchance.Arm(fingerprint="chrome146", profile_name="chrome-macos")
                user = multi_proxy.UserSlot(
                    user_id=7, proxy=p, arm=arm,
                    user_key="a" * 64, attempts=3, last_reward=1.0,
                )
                user.save()
                loaded = json.loads(
                    (Path(tmp) / "user_07.json").read_text()
                )
                self.assertEqual(loaded["user_id"], 7)
                self.assertEqual(loaded["user_key"], "a" * 64)
                self.assertEqual(loaded["proxy"]["proxy"], "1.2.3.4:8080")


class TestResponseClassification(unittest.TestCase):
    def test_status_codes(self):
        for sc, expected_blocked in [
            (200, False), (403, True), (429, True), (500, False),
            (520, True), (404, True),
        ]:
            class _R:
                status_code = sc
                text = ""
            reward, blocked, errored, success, _ = (
                multi_proxy.UserKeyFarm._classify(_R())
            )
            self.assertEqual(blocked, expected_blocked,
                             f"sc={sc} expected_blocked={expected_blocked}")


class TestProxiesTxtFormat(unittest.TestCase):
    def test_extract_writes_one_per_line(self):
        db = multi_proxy.find_proxy_miner_db()
        if db is None:
            self.skipTest("proxy-miner DB not found")
        proxies = multi_proxy.extract_proxies(db, count=50, seed=42)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p.txt"
            multi_proxy.write_proxies_txt(proxies, out)
            content = out.read_text()
            lines = content.strip().split("\n")
            self.assertEqual(len(lines), 50)
            # Every line is ip:port
            for line in lines:
                self.assertIn(":", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
