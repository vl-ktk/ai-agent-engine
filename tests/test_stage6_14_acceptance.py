"""6.14 guarded end-to-end acceptance, offline deterministic workers only."""

import os
import sys
import unittest

if sys.platform.startswith("linux"):
    from ai_agent_engine.task_stage6_14_acceptance import (
        AcceptanceRejected, MARKERS, run_acceptance, main,
    )


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                     "Linux workspace process identity is required")
class Stage614E2ETests(unittest.TestCase):
    def test_two_real_subprocesses_complete_queue_to_version_and_rollback(self):
        checks = run_acceptance(live=False, providers=("gpt", "claude"))
        self.assertEqual([c.provider for c in checks], ["gpt", "claude"])
        self.assertEqual([c.state for c in checks], ["succeeded", "succeeded"])
        self.assertTrue(all(c.rollback_verified for c in checks))
        self.assertTrue(all(c.base_commit == c.result_commit for c in checks))

    def test_each_provider_independently_passes_the_same_flow(self):
        for provider in MARKERS:
            with self.subTest(provider=provider):
                checks = run_acceptance(live=False, providers=(provider,))
                self.assertEqual(len(checks), 1)
                self.assertEqual(checks[0].provider, provider)
                self.assertTrue(checks[0].rollback_verified)

    def test_live_requests_need_explicit_approval_before_side_effects(self):
        with self.assertRaisesRegex(AcceptanceRejected, "owner_approval_required"):
            run_acceptance(live=True, owner_approved=False)
        self.assertEqual(main(["--live", "--provider", "gpt"]), 2)

    def test_invalid_provider_selection_fails_closed(self):
        for invalid in ((), ("gpt", "gpt"), ("unknown",)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AcceptanceRejected):
                    run_acceptance(providers=invalid)


if __name__ == "__main__":
    unittest.main()
