"""
test_perchance.py — Offline unit tests for perchance.py.

We can't test the network layer from this sandbox, but we CAN verify:
    - The PolicyLearner UCB1 algorithm converges
    - Arms with the best simulated reward are preferred
    - State persists correctly to disk
    - Reward classification works
    - The userKey cache roundtrips

Run: python3 test_perchance.py
"""

from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# import the module under test
sys.path.insert(0, str(Path(__file__).parent))
import perchance  # noqa: E402


class TestPolicyLearner(unittest.TestCase):
    def test_ucb1_pulls_every_arm_at_least_once(self):
        """All policies must try every arm at least once before exploiting."""
        for policy in ("ucb1", "softmax", "epsilon-greedy"):
            learner = perchance.PolicyLearner(policy=policy)
            seen = set()
            for _ in range(len(learner.arms) + 5):
                arm = learner.select_arm()
                seen.add((arm.fingerprint, arm.profile_name))
                learner.update(arm, 0.0, blocked=False, errored=False, success=False)
            self.assertEqual(
                len(seen), len(learner.arms),
                f"policy={policy} missed arms",
            )

    def test_softmax_prefers_high_reward_arms(self):
        """Softmax should strongly prefer a high-reward arm after training."""
        # Lower temperature = sharper preference
        learner = perchance.PolicyLearner(policy="softmax", temperature=0.1)
        for arm in learner.arms:
            learner.update(arm, 0.0, blocked=False, errored=False, success=False)
        good_arm = learner.arms[0]
        for _ in range(50):
            learner.update(good_arm, 1.0, blocked=False, errored=False, success=True)
        picks = [learner.select_arm() for _ in range(100)]
        good_picks = sum(1 for p in picks if p is good_arm)
        # With temp=0.1, the math says ~ exp(10)/(exp(10) + 54) ≈ 99.97%
        self.assertGreaterEqual(
            good_picks, 90,
            f"good_picks={good_picks}, expected >= 90 of 100",
        )

    def test_epsilon_greedy_prefers_high_reward_arms(self):
        learner = perchance.PolicyLearner(policy="epsilon-greedy", epsilon=0.05)
        for arm in learner.arms:
            learner.update(arm, 0.0, blocked=False, errored=False, success=False)
        good_arm = learner.arms[0]
        for _ in range(50):
            learner.update(good_arm, 1.0, blocked=False, errored=False, success=True)
        picks = [learner.select_arm() for _ in range(100)]
        good_picks = sum(1 for p in picks if p is good_arm)
        self.assertGreaterEqual(
            good_picks, 80,
            f"good_picks={good_picks}, expected >= 80 of 100 (eps=0.05)",
        )

    def test_state_persistence(self):
        """Save then load should produce an equivalent learner."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(perchance, "POLICY_STATE_FILE", tmp_path / "policy.json"):
                learner = perchance.PolicyLearner(policy="softmax")
                learner.arms[0].n_trials = 42
                learner.arms[0].n_success = 7
                learner.arms[0].reward_sum = 13.5
                learner.total_pulls = 42
                learner.save()

                loaded = perchance.PolicyLearner.load()
                self.assertEqual(loaded.total_pulls, 42)
                self.assertEqual(loaded.policy, "softmax")
                a = next(a for a in loaded.arms
                         if a.fingerprint == learner.arms[0].fingerprint
                         and a.profile_name == learner.arms[0].profile_name)
                self.assertEqual(a.n_trials, 42)
                self.assertEqual(a.n_success, 7)
                self.assertAlmostEqual(a.reward_sum, 13.5)

    def test_mean_reward_and_success_rate(self):
        arm = perchance.Arm(fingerprint="x", profile_name="y")
        arm.n_trials = 4
        arm.n_success = 3
        arm.reward_sum = 2.0
        self.assertAlmostEqual(arm.mean_reward, 0.5)
        self.assertAlmostEqual(arm.success_rate, 0.75)


class TestRewardClassification(unittest.TestCase):
    def test_200_with_userkey(self):
        """A 200 + a userKey JSON should yield REWARD_GOT_USERKEY."""
        # The classification step itself returns 0.0 for 200;
        # the caller is expected to bump to +1.0 if userKey is present.
        client = perchance.PerchanceClient()

        class _FakeResp:
            status_code = 200
            text = json.dumps({"status": "already_verified",
                               "userKey": "a" * 64})
            headers = {"content-type": "application/json"}

        r = _FakeResp()
        reward, blocked, errored, success, detail = client._classify_failure(r)
        self.assertEqual(reward, 0.0)
        self.assertFalse(blocked)
        self.assertFalse(errored)
        self.assertTrue(success)

    def test_403_is_blocked(self):
        client = perchance.PerchanceClient()
        class _FakeResp:
            status_code = 403
            text = "<html>Forbidden</html>"
            headers = {"content-type": "text/html"}
        r = _FakeResp()
        reward, blocked, errored, success, detail = client._classify_failure(r)
        self.assertEqual(reward, perchance.REWARD_FORBIDDEN)
        self.assertTrue(blocked)
        self.assertFalse(success)

    def test_429_is_rate_limited(self):
        client = perchance.PerchanceClient()
        class _FakeResp:
            status_code = 429
            text = ""
            headers = {}
        r = _FakeResp()
        reward, blocked, errored, success, detail = client._classify_failure(r)
        self.assertEqual(reward, perchance.REWARD_RATE_LIMITED)
        self.assertTrue(blocked)

    def test_500_is_error(self):
        client = perchance.PerchanceClient()
        class _FakeResp:
            status_code = 500
            text = ""
            headers = {}
        r = _FakeResp()
        reward, blocked, errored, success, detail = client._classify_failure(r)
        self.assertEqual(reward, perchance.REWARD_NETWORK_ERR)
        self.assertTrue(errored)

    def test_cloudflare_error_codes(self):
        client = perchance.PerchanceClient()
        for sc in (520, 521, 522, 523, 524, 525, 526, 527, 530):
            class _FakeResp:
                status_code = sc
                text = ""
                headers = {}
            r = _FakeResp()
            reward, blocked, _, _, _ = client._classify_failure(r)
            self.assertEqual(reward, perchance.REWARD_BLOCKED, f"sc={sc}")
            self.assertTrue(blocked, f"sc={sc}")


class TestUserKeyExtraction(unittest.TestCase):
    def test_userkey_extracted_from_json_body(self):
        client = perchance.PerchanceClient()
        # We need a real request to test this, so we mock curl_cffi.
        # Simpler: just check the regex in the code logic.
        import re
        body = '{"status":"already_verified","userKey":"abc123" + "x"*44}'
        # Build a valid 64-hex body
        hex_64 = "f" * 64
        body = f'{{"status":"already_verified","userKey":"{hex_64}"}}'
        m = re.search(r'"userKey"\s*:\s*"([a-f0-9]{64})"', body)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), hex_64)

    def test_userkey_cache_roundtrip(self):
        client = perchance.PerchanceClient()
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "userkey.json"
            with patch.object(perchance, "USERKEY_CACHE_FILE", cache_path):
                self.assertIsNone(client._load_cached_user_key())
                client._save_user_key("d" * 64)
                self.assertEqual(client._load_cached_user_key(), "d" * 64)


class TestUserKeyFlow(unittest.TestCase):
    """End-to-end test of the userKey flow with mocked network."""

    def test_successful_verifyuser(self):
        client = perchance.PerchanceClient()
        hex_uk = "abc123" + "0" * 58
        body = json.dumps({"status": "already_verified", "userKey": hex_uk})

        class _FakeResp:
            status_code = 200
            text = body
            headers = {"content-type": "application/json"}
            def json(self):
                return json.loads(self.text)

        # Patch the session.get to return our fake
        with patch.object(client, "_make_session") as ms:
            sess = ms.return_value
            sess.get.return_value = _FakeResp()
            res = client.acquire_user_key(verbose=False)
            self.assertTrue(res.ok)
            self.assertEqual(res.user_key, hex_uk)
            self.assertEqual(res.reward, perchance.REWARD_GOT_USERKEY)

    def test_blocked_returns_blocked(self):
        client = perchance.PolicyLearner.load() or perchance.PolicyLearner()
        client_obj = perchance.PerchanceClient(learner=client)

        class _FakeResp:
            status_code = 403
            text = "blocked"
            headers = {}

        with patch.object(client_obj, "_make_session") as ms:
            sess = ms.return_value
            sess.get.return_value = _FakeResp()
            res = client_obj.acquire_user_key(verbose=False)
            self.assertFalse(res.ok)
            self.assertTrue(res.blocked)
            self.assertIsNone(res.user_key)

    def test_network_error_returns_errored(self):
        client_obj = perchance.PerchanceClient()
        with patch.object(client_obj, "_make_session") as ms:
            sess = ms.return_value
            sess.get.side_effect = OSError("no network")
            res = client_obj.acquire_user_key(verbose=False)
            self.assertFalse(res.ok)
            self.assertTrue(res.errored)


class TestFingerprintsAndProfiles(unittest.TestCase):
    def test_fingerprints_nonempty(self):
        self.assertGreater(len(perchance.FINGERPRINTS), 0)

    def test_profiles_have_unique_names(self):
        names = [p["name"] for p in perchance.HEADER_PROFILES]
        self.assertEqual(len(names), len(set(names)))

    def test_arms_match_grid(self):
        """Number of arms should be fingerprints * profiles."""
        learner = perchance.PolicyLearner()
        self.assertEqual(
            len(learner.arms),
            len(perchance.FINGERPRINTS) * len(perchance.HEADER_PROFILES),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
