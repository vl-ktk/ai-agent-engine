"""Regression tests for Stage 4.2/6.4 workflow source policy."""
import unittest

from scripts.check_workflow_boundaries import inspect_repository, inspect_workflow


class WorkflowBoundaryTests(unittest.TestCase):
    def test_live_repository_policy(self):
        self.assertEqual(inspect_repository(), [])

    def test_reject_token_write(self):
        self.assertTrue(
            inspect_workflow("ci.yml", "permissions:\n  contents: write\n")
        )

    def test_reject_nested_permission_override(self):
        self.assertTrue(
            inspect_workflow(
                "ci.yml",
                "permissions:\n  contents: read\n"
                "jobs:\n  test:\n    permissions: write-all\n",
            )
        )

    def test_reject_secret_in_automatic_workflow(self):
        source = (
            "on:\n  pull_request:\n  workflow_dispatch:\n"
            "permissions:\n  contents: read\n"
            "jobs:\n  test:\n"
            "    if: ${{ github.ref == 'refs/heads/main' && "
            "github.actor == 'vl-ktk' && inputs.confirmation == 'VERIFY' }}\n"
            "    steps:\n      - run: ${{ secrets.AE_DEPLOY_PRIVATE_KEY }}\n"
        )
        self.assertTrue(inspect_workflow("netcup-readonly-ansible-ping.yml", source))

    def test_retired_bootstrap_cannot_be_reenabled_silently(self):
        source = (
            "on:\n  workflow_dispatch:\n"
            "permissions:\n  contents: read\n"
            "jobs:\n  create-deploy-user:\n"
            "    if: ${{ github.ref == 'refs/heads/main' && "
            "github.actor == 'vl-ktk' && inputs.confirmation == 'BOOTSTRAP' }}\n"
            "    steps:\n      - run: ${{ secrets.NETCUP_ROOT_BOOTSTRAP_PASSWORD }}\n"
        )
        self.assertTrue(inspect_workflow("netcup-one-time-bootstrap.yml", source))


    def test_release_preparation_rejects_automatic_triggers(self):
        source = (
            "on:\n  push:\n  workflow_dispatch:\n"
            "permissions:\n  contents: read\n"
            "jobs:\n  prepare-release:\n"
            "    if: ${{ github.ref == 'refs/heads/main' && "
            "github.actor == 'vl-ktk' && inputs.confirmation == 'PREPARE_RELEASE' }}\n"
            "    steps:\n      - run: echo unsafe\n"
        )
        self.assertTrue(inspect_workflow("release-preparation.yml", source))

    def test_release_preparation_rejects_unreviewed_owner_condition(self):
        source = (
            "on:\n  workflow_dispatch:\n"
            "permissions:\n  contents: read\n"
            "jobs:\n  prepare-release:\n"
            "    if: ${{ github.ref == 'refs/heads/main' }}\n"
            "    steps:\n      - run: echo unsafe\n"
        )
        findings = inspect_workflow("release-preparation.yml", source)
        self.assertTrue(any("owner gate" in issue for issue in findings))


if __name__ == "__main__":
    unittest.main()
