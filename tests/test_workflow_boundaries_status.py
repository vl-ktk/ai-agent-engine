"""Regression tests for the narrow automatic SSH status exception."""
from pathlib import Path
import unittest

from scripts.check_workflow_boundaries import inspect_workflow


WORKFLOW = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
            / "netcup-gitops-status.yml")


class StatusWorkflowPolicyTests(unittest.TestCase):
    def test_status_workflow_passes_reviewed_policy(self):
        self.assertEqual(inspect_workflow(WORKFLOW.name, WORKFLOW.read_text()), [])

    def test_reject_status_without_owner_gate(self):
        code = WORKFLOW.read_text().replace("github.actor == 'vl-ktk'",
                                           "github.actor == 'anyone'")
        self.assertTrue(inspect_workflow(WORKFLOW.name, code))

    def test_reject_status_scope_broadening(self):
        code = WORKFLOW.read_text().replace(
            "'ops/requests/*.json'", "'**/*.json'")
        self.assertTrue(inspect_workflow(WORKFLOW.name, code))

    def test_reject_automatic_sudo(self):
        code = WORKFLOW.read_text() + "\n      - run: sudo systemctl restart ae-controller.service\n"
        self.assertTrue(inspect_workflow(WORKFLOW.name, code))

    def test_reject_nonmanual_activation(self):
        activation = (WORKFLOW.parent / "netcup-owner-release-activate.yml").read_text()
        activation = activation.replace("  workflow_dispatch:", "  push:")
        self.assertTrue(inspect_workflow("netcup-owner-release-activate.yml", activation))

    def test_reject_activation_without_separate_signing_key(self):
        activation = (WORKFLOW.parent / "netcup-owner-release-activate.yml").read_text()
        changed = activation.replace("secrets.AE_RELEASE_SIGNING_PRIVATE_KEY",
                                     "secrets.AE_DEPLOY_PRIVATE_KEY")
        findings = inspect_workflow("netcup-owner-release-activate.yml", changed)
        self.assertTrue(any("release signing boundary" in x for x in findings))

    def test_reject_status_without_strict_unit_health(self):
        source = WORKFLOW.read_text()
        changed = source.replace(
            "systemctl is-active --quiet ae-backup.timer",
            "systemctl is-active ae-backup.timer")
        self.assertTrue(inspect_workflow(WORKFLOW.name, changed))


if __name__ == "__main__":
    unittest.main()
