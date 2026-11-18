"""Read-only auth preflight parser tests, no credentials or model calls."""
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "ops" / "admin-scripts" / "stage6_14_preflight.py"
SPEC = importlib.util.spec_from_file_location("stage6_14_netup_preflight", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Stage614NetcupPreflightTests(unittest.TestCase):
    def test_gpt_absent(self):
        self.assertEqual(MODULE.gpt_status("Not signed in on this computer.", 0),
                         {"status_usable": True, "saved_session": False, "plan_granted": False})

    def test_gpt_grant(self):
        self.assertTrue(MODULE.gpt_status(
            "Saved ChatGPT identity: REDACTED\nChatGPT plan permission: GRANTED", 0
        )["plan_granted"])

    def test_gpt_failed(self):
        self.assertFalse(MODULE.gpt_status(
            "Saved ChatGPT identity: fake\nChatGPT plan permission: GRANTED", 2
        )["plan_granted"])

    def test_claude_pro(self):
        self.assertTrue(MODULE.claude_status(
            '{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"pro"}', 0
        )["subscription_eligible"])

    def test_claude_reject_api_key(self):
        self.assertFalse(MODULE.claude_status(
            '{"loggedIn":true,"authMethod":"api_key","subscriptionType":"pro"}', 0
        )["subscription_eligible"])

    def test_claude_reject_failed_cli(self):
        self.assertFalse(MODULE.claude_status(
            '{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"pro"}', 2
        )["subscription_eligible"])

    def test_claude_reject_bad_data(self):
        self.assertFalse(MODULE.claude_status("not-json", 0)["subscription_eligible"])


if __name__ == "__main__":
    unittest.main()
