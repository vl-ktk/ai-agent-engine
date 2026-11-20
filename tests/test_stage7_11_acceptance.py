"""7.11: the live acceptance can only run with separate trusted approval."""

from __future__ import annotations

from io import StringIO
import json
import sys
import unittest
from unittest.mock import patch

if sys.platform.startswith("linux"):
    from ai_agent_engine import task_stage7_11_acceptance as acceptance


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private SQLite only")
class Stage711AcceptanceTests(unittest.TestCase):
    def test_real_offline_child_processes_and_v17_capacity(self):
        result = acceptance.run_acceptance()
        self.assertEqual(result["status"], "OFFLINE_PASS")
        self.assertEqual(result["providers"], ["gpt", "claude"])
        self.assertEqual(result["cpu_slot_wait"], "PASS")
        self.assertEqual(result["provider_quota_wait"], "PASS")
        self.assertTrue(result["delivery_not_spent_on_wait"])
        self.assertTrue(result["durable_sqlite_v17"])
        self.assertEqual(result["live_provider_calls"], 0)

    def test_live_without_owner_consent_fails_before_creating_state(self):
        with patch.object(acceptance.tempfile, "TemporaryDirectory") as tmp:
            with self.assertRaisesRegex(acceptance.AcceptanceBlocked,
                                        "explicit_owner_approval_required"):
                acceptance.run_acceptance(live=True)
            tmp.assert_not_called()

    def test_default_cli_only_runs_offline(self):
        output = StringIO()
        with patch("sys.stdout", output):
            rc = acceptance.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue().split(" ", 1)[1])["live_provider_calls"], 0)

    def test_cli_refuses_live_without_approval(self):
        output = StringIO()
        with patch("sys.stdout", output):
            rc = acceptance.main(["--live"])
        self.assertEqual(rc, 2)
        self.assertIn("STAGE7_11_LIVE BLOCKED", output.getvalue())

    def test_explicit_approval_flag_without_live_does_not_call_model(self):
        outcome = acceptance.run_acceptance(owner_approved=True)
        self.assertEqual(outcome["status"], "OFFLINE_PASS")
        self.assertEqual(outcome["live_provider_calls"], 0)

    def test_sanitized_failure_never_prints_error_details(self):
        out = StringIO()
        with patch.object(acceptance, "run_acceptance",
                          side_effect=ValueError("private owner account: token")):
            with patch("sys.stdout", out):
                self.assertEqual(acceptance.main([]), 2)
        self.assertEqual(out.getvalue(),
                         "STAGE7_11_OFFLINE BLOCKED: private acceptance failed; no output disclosed\n")


if __name__ == "__main__":
    unittest.main()
