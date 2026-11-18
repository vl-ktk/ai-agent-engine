"""Offline release staging and atomic rollback checks in disposable directories."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile
from unittest.mock import patch

from infra.deploy.promotion import (
    PromotionError, active_sha, main, promote_release, require_sha,
    stage_release, verified_bundle,
)


FIRST = "a" * 40
SECOND = "b" * 40


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "ai-agent-engine"
        (self.root / "releases").mkdir(parents=True)
        self.bundle = self.base / "bundle"
        self.bundle.mkdir()
        self.wheel = self.bundle / "ai_agent_engine-0.3.8a0-py3-none-any.whl"
        self.wheel.write_bytes(b"synthetic wheel only for hash validation")
        self.manifest = self.bundle / "release-manifest.json"
        self.data = {
            "schema": 1, "mode": "prepared_only_no_vps_deploy",
            "source_commit": FIRST, "validated_main_head": FIRST,
            "controller_service_activated": False,
            "wheel_file": self.wheel.name, "wheel_bytes": self.wheel.stat().st_size,
            "wheel_sha256": hashlib.sha256(self.wheel.read_bytes()).hexdigest(),
        }
        self._save()
        self.digest = self.data["wheel_sha256"]

    def _save(self):
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def _fake_release(self, sha):
        target = self.root / "releases" / sha
        (target / "venv" / "bin").mkdir(parents=True)
        (target / "venv" / "bin" / "python").write_text("synthetic python")
        return target

    def test_verified_bundle_refuses_corruption_and_mismatched_commit(self):
        self.assertEqual(verified_bundle(self.bundle, FIRST)[0], self.wheel)
        with self.assertRaises(PromotionError):
            verified_bundle(self.bundle, FIRST, expected_wheel_sha256="0" * 64)
        with self.assertRaises(PromotionError):
            verified_bundle(self.bundle, SECOND)
        self.wheel.write_bytes(b"tampered")
        with self.assertRaises(PromotionError):
            verified_bundle(self.bundle, FIRST)
        self.wheel.write_bytes(b"synthetic wheel only for hash validation")
        self.data["mode"] = "installed"
        self._save()
        with self.assertRaises(PromotionError):
            verified_bundle(self.bundle, FIRST)

    def test_verify_is_read_only_and_modifications_need_confirmation(self):
        self.assertEqual(main(["verify", "--bundle", str(self.bundle),
                               "--source-sha", FIRST,
                               "--expected-wheel-sha256", self.digest]), 0)
        self.assertEqual(main(["stage", "--bundle", str(self.bundle),
                               "--source-sha", FIRST, "--root", str(self.root),
                               "--expected-wheel-sha256", self.digest,
                               "--confirmation", "UNAUTHORIZED"]), 1)
        self.assertEqual(list((self.root / "releases").iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "Atomic promotion uses POSIX flock")
    def test_promote_requires_exact_current_and_supports_rollback(self):
        self._fake_release(FIRST)
        self._fake_release(SECOND)
        self.assertIsNone(active_sha(self.root))
        self.assertIsNone(promote_release(self.root, FIRST, expected_current=None))
        self.assertEqual(active_sha(self.root), FIRST)
        with self.assertRaises(PromotionError):
            promote_release(self.root, SECOND, expected_current=None)
        self.assertEqual(promote_release(self.root, SECOND, expected_current=FIRST), FIRST)
        self.assertEqual(active_sha(self.root), SECOND)
        self.assertEqual(promote_release(self.root, FIRST, expected_current=SECOND), SECOND)
        self.assertEqual(active_sha(self.root), FIRST)

    @unittest.skipUnless(os.name == "posix", "Symlink operations require POSIX")
    def test_rejects_arbitrary_roots_and_current_escape(self):
        with self.assertRaises(PromotionError):
            active_sha(self.base)
        self._fake_release(FIRST)
        (self.root / "current").symlink_to("../../outside")
        with self.assertRaises(PromotionError):
            promote_release(self.root, FIRST, expected_current=None)
        (self.root / "current").unlink()
        (self.root / "current").mkdir()
        with self.assertRaises(PromotionError):
            promote_release(self.root, FIRST, expected_current=None)

    def test_stage_rejects_preexisting_release_without_invoking_installer(self):
        self._fake_release(FIRST)
        with patch("infra.deploy.promotion.subprocess.run") as installer:
            with self.assertRaises(PromotionError):
                stage_release(self.bundle, self.root, FIRST,
                              expected_wheel_sha256=self.digest)
            installer.assert_not_called()

    def test_stage_rejects_unreviewed_independent_digest(self):
        with patch("infra.deploy.promotion.subprocess.run") as installer:
            with self.assertRaises(PromotionError):
                stage_release(self.bundle, self.root, FIRST,
                              expected_wheel_sha256="0" * 64)
            installer.assert_not_called()


    @unittest.skipUnless(os.name == "posix", "Release permissions require POSIX")
    def test_real_staging_is_runtime_readable_under_private_admin_umask(self):
        # A real minimal wheel, venv and pip install reproduce root-job umask 077.
        with zipfile.ZipFile(self.wheel, "w") as wheel:
            wheel.writestr("ai_agent_engine/__init__.py", "")
            wheel.writestr("ai_agent_engine/controller.py",
                          "def check_health(*args): return False\n"
                          "def main(args): return 1\n")
            prefix = "ai_agent_engine-0.3.8a0.dist-info/"
            wheel.writestr(prefix + "METADATA",
                          "Metadata-Version: 2.1\nName: ai-agent-engine\nVersion: 0.3.8a0\n")
            wheel.writestr(prefix + "WHEEL",
                          "Wheel-Version: 1.0\nGenerator: unittest\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            wheel.writestr(prefix + "RECORD", "")
        self.data["wheel_bytes"] = self.wheel.stat().st_size
        self.digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self.data["wheel_sha256"] = self.digest
        self._save()
        previous_umask = os.umask(0o077)
        try:
            target = stage_release(self.bundle, self.root, FIRST,
                                   expected_wheel_sha256=self.digest)
        finally:
            os.umask(previous_umask)
        self.assertFalse((self.root / "current").exists())
        for path in (target, *target.rglob("*")):
            if path.is_symlink():
                continue  # venv links to the runner interpreter; it is not a staged file.
            mode = path.stat().st_mode & 0o777
            self.assertFalse(mode & 0o022, str(path))
            if path.is_dir():
                self.assertEqual(mode & 0o005, 0o005, str(path))
            elif path.is_file():
                self.assertTrue(mode & 0o004, str(path))

    def test_requires_exact_sha(self):
        for bad in ("", "main", "A" * 40, "a" * 39, "a" * 41):
            with self.subTest(bad=bad), self.assertRaises(PromotionError):
                require_sha(bad)


if __name__ == "__main__":
    unittest.main()
