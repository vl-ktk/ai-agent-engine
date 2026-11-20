"""Offline pin/security tests for owner-signed isolated 7.7 v2 upgrade."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from threading import Event
from zipfile import ZipFile
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only signed host gate")
class SignedDiagnosticsV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.op = load("infra/deploy/diagnostic_addon_operator_v2.py", "ae_v2_operator")
        cls.up = load("ops/admin-scripts/upgrade_managed_diagnostics_7_7_v2.py", "ae_v2_upgrade")
        cls.gate = load("ops/admin-scripts/authorize_diagnostic_v2_gate.py", "ae_v2_bootstrap")

    def test_old_root_operator_remains_frozen_and_v2_is_exact(self):
        old = ROOT / "infra/deploy/diagnostic_addon_operator.py"
        self.assertEqual(sha256(old.read_bytes()).hexdigest(), self.gate.OLD_OPERATOR_SHA)
        self.assertEqual(sha256((ROOT / "infra/deploy/diagnostic_addon_operator_v2.py").read_bytes()).hexdigest(),
                         self.gate.NEW_OPERATOR_SHA)
        self.assertEqual(sha256((ROOT / "ops/admin-scripts/upgrade_managed_diagnostics_7_7_v2.py").read_bytes()).hexdigest(),
                         self.op.INSTALLER_SHA256)
        self.assertEqual(self.op.INSTALLER_SHA256, self.gate.NEW_INSTALLER_SHA)
        self.assertEqual(self.op.SOURCE_SHA, "3ef07c51ec287a24bd54d8fcea7c40a99e69b569")
        self.assertEqual(self.op.LIVE_SHA, self.up.LIVE_SHA)
        self.assertEqual(self.op.RUNTIME_SHA256,
                         sha256((ROOT / "ai_agent_engine/diagnostic_managed_runtime.py").read_bytes()).hexdigest())

    def test_only_exact_new_source_and_old_controller(self):
        for source, old in (("0" * 40, self.op.LIVE_SHA),
                            (self.op.SOURCE_SHA, "0" * 40)):
            with self.assertRaisesRegex(self.op.AddonRefused, "unapproved"):
                self.op._verify_intent(source_sha=source, current=old, wheel_sha256="a" * 64)
        self.assertEqual(self.op.OPERATION, "upgrade-managed-diagnostics-7-7-v2")
        self.assertEqual(self.op.CONFIRMATION, "UPGRADE_SIGNED_DIAGNOSTICS_7_7_V2")

    def test_operator_entry_point_invokes_only_pinned_upgrade(self):
        argv = ["addon-operator", "--source-sha", self.op.SOURCE_SHA,
                "--expected-current", self.op.LIVE_SHA,
                "--wheel-sha256", "a" * 64,
                "--confirmation", self.op.CONFIRMATION]
        with (patch.object(sys, "argv", argv),
              patch.object(self.op.os, "geteuid", return_value=0),
              patch.object(self.op, "upgrade") as signed_upgrade):
            self.assertEqual(self.op.main(), 0)
            signed_upgrade.assert_called_once_with(
                source_sha=self.op.SOURCE_SHA,
                expected_current=self.op.LIVE_SHA,
                wheel_sha256="a" * 64)

    def test_operator_extracts_only_exact_reviewed_candidate(self):
        package = ROOT / "ai_agent_engine"
        raw = BytesIO()
        with ZipFile(raw, "w") as wheel:
            for path in sorted(package.glob("*.py")):
                wheel.write(path, "ai_agent_engine/" + path.name)
        with tempfile.TemporaryDirectory() as tmp:
            count = self.op._extract_package(raw.getvalue(), Path(tmp))
            self.assertGreaterEqual(count, 50)
            self.assertEqual(sha256((Path(tmp) / "ai_agent_engine/diagnostic_managed_runtime.py").read_bytes()).hexdigest(),
                             self.op.RUNTIME_SHA256)

    def test_one_shot_qa_is_fixed_and_service_stays_hardened(self):
        qa = self.up.qa_launcher_text()
        self.assertIn("negative_check=True", qa)
        self.assertIn("QA_NEGATIVE_PROPOSAL_SHA256", qa)
        self.assertIn("stop.set()", qa)
        self.assertIn("'message'", qa)
        self.assertNotIn("sys.argv", qa)
        transient = self.up.qa_unit_text()
        regular = self.up.main_unit_text()
        for unit in (transient, regular):
            self.assertIn("PrivateNetwork=yes", unit)
            self.assertIn("NoNewPrivileges=yes", unit)
            self.assertIn("RestrictAddressFamilies=AF_UNIX", unit)
            self.assertIn("User=ae-runtime", unit)
            self.assertIn("MemoryMax=192M", unit)
            self.assertNotIn("ae-controller.service", unit)
        self.assertIn("Type=oneshot", transient)
        self.assertNotIn("[Install]", transient)
        self.assertIn("Type=simple", regular)
        self.assertIn("Restart=no", regular)
        self.assertIn("/opt/ai-agent-engine/diagnostic-7-7-v2", regular)

    def test_bootstrap_does_not_change_old_installer_signers_or_sudo(self):
        self.assertNotEqual(self.gate.ROOT_OPERATOR, self.gate.NEW_INSTALLER)
        self.assertEqual(self.gate.ROOT_OPERATOR.name, "diagnostic_addon_operator.py")
        self.assertEqual(self.gate.OLD_INSTALLER.name, "activate_managed_diagnostics_7_7.py")
        self.assertEqual(self.gate.NEW_INSTALLER.name, "upgrade_managed_diagnostics_7_7_v2.py")
        workflow = (ROOT / ".github/workflows/netcup-owner-diagnostics-upgrade-v2.yml").read_text()
        self.assertIn("github.actor == 'vl-ktk'", workflow)
        self.assertIn("github.triggering_actor == 'vl-ktk'", workflow)
        self.assertIn("secrets.AE_RELEASE_SIGNING_PRIVATE_KEY", workflow)
        self.assertIn("'3ef07c51ec287a24bd54d8fcea7c40a99e69b569'", workflow)
        self.assertNotIn("60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23", workflow)

    def test_exact_host_qa_readback_on_real_disposable_sqlite(self):
        """One real negative Git child + the *installer's* persisted proof."""
        from tests.test_diagnostic_managed_runtime_7_7 import ManagedRuntimeTests
        from ai_agent_engine.diagnostic_managed_runtime import (
            OwnerConfirmedDiagnosticQA, QA_NEGATIVE_PROPOSAL_SHA256,
            run_managed,
        )
        from ai_agent_engine.task_api import OwnerApprovalProof

        fixture = ManagedRuntimeTests(methodName="test_runner_is_inert_and_receives_existing_adapter")
        fixture.setUp()
        try:
            qa = OwnerConfirmedDiagnosticQA(OwnerApprovalProof(
                "proposal_" + self.up.SOURCE_SHA[:32], "message",
                QA_NEGATIVE_PROPOSAL_SHA256,
            ), negative_check=True)
            with patch("ai_agent_engine.diagnostic_managed_runtime.serve_local_intake"):
                run_managed(fixture.config, Event(), expected_project_source=fixture.sha,
                            qa=qa)
            with patch.object(self.up, "PRIVATE_ROOT", fixture.root):
                report = self.up._verified_host_qa(os.getuid())
            self.assertEqual(report["status"], "SIGNED_REAL_SUBPROCESS_QA_FAILED_AS_EXPECTED")
            self.assertTrue(report["qa_task_id"].startswith("task_"))
            self.assertTrue(report["qa_incident_id"].startswith("inc_"))
            self.assertEqual(fixture.git("status", "--porcelain"), "")
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
