"""Fail-closed tests for the separately signed 7.7 release channel."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def load(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux release operator")
class DiagnosticSigningGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.op = load("infra/deploy/diagnostic_addon_operator.py", "ae_diagnostic_addon")
        cls.boot = load("ops/admin-scripts/bootstrap_diagnostic_signing_7_7.py", "ae_diagnostic_bootstrap")

    def _incoming(self, base, *, operation=None, expires=None):
        op = self.op
        source = Path(base) / "incoming" / op.SOURCE_SHA
        source.mkdir(parents=True)
        tick = int(time.time())
        doc = {"schema": 1, "operation": operation or op.OPERATION,
               "source_sha": op.SOURCE_SHA, "expected_current": op.LIVE_SHA,
               "wheel_sha256": "a" * 64, "issued_at": tick,
               "expires_at": tick + 900 if expires is None else expires}
        (source / "release-intent.json").write_text(json.dumps(doc))
        (source / "release-intent.json.sig").write_bytes(b"fake-signature")
        return source

    def test_signer_is_public_and_matches_workflow(self):
        self.assertTrue(self.boot.PUBLIC_SIGNER.startswith("ai-agent-engine-owner ssh-ed25519 "))
        self.assertNotIn("PRIVATE", self.boot.PUBLIC_SIGNER)
        workflow = (ROOT / ".github/workflows/netcup-owner-diagnostics-activate.yml").read_text()
        self.assertIn(self.boot.PUBLIC_SIGNER.strip(), workflow)
        self.assertIn("secrets.AE_RELEASE_SIGNING_PRIVATE_KEY", workflow)
        self.assertIn("github.actor == 'vl-ktk'", workflow)
        self.assertIn("github.triggering_actor == 'vl-ktk'", workflow)
        self.assertNotIn("netcup-owner-release-activate.yml", workflow)
        self.assertEqual(sha256(self.boot.PUBLIC_SIGNER.encode()).hexdigest(),
                         self.op.SIGNERS_SHA256)
        self.assertEqual(sha256((ROOT / self.boot.SOURCE_OPERATOR).read_bytes()).hexdigest(),
                         self.boot.OPERATOR_SHA256)
        self.assertEqual(sha256((ROOT / self.boot.SOURCE_INSTALLER).read_bytes()).hexdigest(),
                         self.boot.INSTALLER_SHA256)
        self.assertEqual(self.op.INSTALLER_SHA256, self.boot.INSTALLER_SHA256)

    def test_only_exact_reviewed_source_and_old_release_allowed(self):
        op = self.op
        for params in (
            ("1" * 40, op.LIVE_SHA, "a" * 64),
            (op.SOURCE_SHA, "1" * 40, "a" * 64),
            (op.SOURCE_SHA, op.LIVE_SHA, "not-a-digest"),
        ):
            with self.assertRaisesRegex(op.AddonRefused, "unapproved"):
                op._verify_intent(source_sha=params[0], current=params[1],
                                  wheel_sha256=params[2])

    def test_wrong_operation_and_expiry_fail_before_signature(self):
        op = self.op
        with tempfile.TemporaryDirectory() as temp:
            source = self._incoming(temp, operation="activate")
            with self.assertRaisesRegex(op.AddonRefused, "does not authorize"):
                op._verify_intent(source_sha=op.SOURCE_SHA, current=op.LIVE_SHA,
                                  wheel_sha256="a" * 64, incoming=source.parent)
            doc = json.loads((source / "release-intent.json").read_text())
            doc["operation"] = op.OPERATION
            doc["expires_at"] = 1
            (source / "release-intent.json").write_text(json.dumps(doc))
            with self.assertRaisesRegex(op.AddonRefused, "expired"):
                op._verify_intent(source_sha=op.SOURCE_SHA, current=op.LIVE_SHA,
                                  wheel_sha256="a" * 64, incoming=source.parent)

    def test_valid_shape_reaches_ssh_signature_check_and_fails_closed(self):
        op = self.op
        with tempfile.TemporaryDirectory() as temp:
            source = self._incoming(temp)
            releases = Path(temp) / "releases"
            releases.mkdir()
            signers = Path(temp) / "signers"
            signers.write_text(self.boot.PUBLIC_SIGNER)
            with patch.object(op, "_root_file"), patch.object(op, "_root_directory"), \
                 patch.object(op.subprocess, "run", side_effect=op.subprocess.CalledProcessError(1, "verify")):
                with self.assertRaisesRegex(op.AddonRefused, "invalid owner signature"):
                    op._verify_intent(source_sha=op.SOURCE_SHA, current=op.LIVE_SHA,
                                      wheel_sha256="a" * 64, incoming=source.parent,
                                      signers=signers, releases=releases)

    def test_signed_wheel_digest_must_match_manifest_and_bytes(self):
        op = self.op
        with tempfile.TemporaryDirectory() as temp:
            source = self._incoming(temp)
            archive = b"wheel payload"
            digest = sha256(archive).hexdigest()
            manifest = {"schema": 1, "source_commit": op.SOURCE_SHA,
                        "wheel_file": "ai_agent_engine-7.7-py3-none-any.whl",
                        "wheel_sha256": digest}
            (source / "release-manifest.json").write_text(json.dumps(manifest))
            (source / manifest["wheel_file"]).write_bytes(archive)
            self.assertEqual(op._wheel_bytes(source_sha=op.SOURCE_SHA,
                                              wheel_sha256=digest,
                                              incoming=source.parent), archive)
            (source / manifest["wheel_file"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(op.AddonRefused, "digest differs"):
                op._wheel_bytes(source_sha=op.SOURCE_SHA, wheel_sha256=digest,
                                incoming=source.parent)

    def test_extract_only_reviewed_python_package_and_reject_duplicate(self):
        op = self.op
        package = ROOT / "ai_agent_engine"
        # The installed v1 signed operator intentionally remains pinned to
        # the historical runtime. Never weaken it to accept a future 7.7 QA
        # candidate merely because main now has a different module body.
        reviewed_v1 = (ROOT / "tests/fixtures/diagnostic_managed_runtime_v1.py.txt").read_bytes()
        self.assertEqual(sha256(reviewed_v1).hexdigest(), op.RUNTIME_SHA256)
        with tempfile.TemporaryDirectory() as temp:
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                for source in sorted(package.glob("*.py")):
                    target = "ai_agent_engine/" + source.name
                    if source.name == "diagnostic_managed_runtime.py":
                        archive.writestr(target, reviewed_v1)
                    else:
                        archive.write(source, target)
                archive.writestr("ai_agent_engine-0.3.19a0.dist-info/entry_points.txt", "ignored")
            count = op._extract_package(buffer.getvalue(), Path(temp))
            self.assertGreaterEqual(count, 20)
            self.assertFalse((Path(temp) / "ai_agent_engine-0.3.19a0.dist-info").exists())
            self.assertEqual(sha256((Path(temp) / "ai_agent_engine" / "diagnostic_managed_runtime.py").read_bytes()).hexdigest(),
                             op.RUNTIME_SHA256)
            duplicate = BytesIO()
            with ZipFile(duplicate, "w") as archive:
                archive.writestr("ai_agent_engine/fake.py", "first")
                archive.writestr("ai_agent_engine/fake.py", "second")
            (Path(temp) / "new").mkdir()
            with self.assertRaisesRegex(op.AddonRefused, "duplicate wheel member"):
                op._extract_package(duplicate.getvalue(), Path(temp) / "new")

    def test_bootstrap_rejects_existing_targets_before_any_writes(self):
        boot = self.boot
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / "evidence"
            evidence.mkdir()
            with patch.object(boot, "_baseline"), \
                 patch.object(boot, "_check_evidence"), \
                 patch.object(boot, "_source_bytes", return_value=b"trusted"), \
                 patch.object(boot, "SIGNERS", evidence / "already-here"), \
                 patch.object(boot, "OPERATOR", evidence / "operator"), \
                 patch.object(boot, "INSTALLER", evidence / "installer"):
                (evidence / "already-here").write_bytes(b"do not replace")
                with self.assertRaisesRegex(RuntimeError, "target_already_present"):
                    boot.bootstrap(expected=boot.LIVE_SHA, evidence=evidence)
                self.assertEqual((evidence / "already-here").read_bytes(), b"do not replace")

    def test_bootstrap_preserves_exact_modes_under_restrictive_owner_umask(self):
        boot = self.boot
        with tempfile.TemporaryDirectory() as temp:
            for mode in (0o444, 0o644):
                target = Path(temp) / ("new-" + oct(mode))
                tracked = []
                previous = os.umask(0o077)
                try:
                    boot._exclusive_root_file(target, b"reviewed", mode,
                                              created=tracked,
                                              expected_uid=os.getuid())
                finally:
                    os.umask(previous)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
                self.assertEqual(tracked, [target])
                self.assertEqual(target.read_bytes(), b"reviewed")
                with self.assertRaises(FileExistsError):
                    boot._exclusive_root_file(target, b"overwrite", mode,
                                              expected_uid=os.getuid())
                self.assertEqual(target.read_bytes(), b"reviewed")


if __name__ == "__main__":
    unittest.main()
