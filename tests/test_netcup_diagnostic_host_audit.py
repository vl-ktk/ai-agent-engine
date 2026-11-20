"""Offline tests for the immutable owner-admin 7.7 host audit script."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


SCRIPT = (Path(__file__).resolve().parents[1] / "ops" / "admin-scripts"
          / "diagnostic_host_audit_7_7.py")
SPEC = importlib.util.spec_from_file_location("diagnostic_host_audit_7_7", SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class DiagnosticHostAuditTests(unittest.TestCase):
    def test_cpu_percent_from_realistic_aggregate_samples(self):
        self.assertEqual(AUDIT._cpu_busy(
            "cpu  100 0 100 800 0 0 0 0 0 0",
            "cpu  120 0 120 860 0 0 0 0 0 0"), 40.0)
        self.assertEqual(AUDIT._cpu_busy(
            "cpu  100 0 100 800 0 0 0 0 100 0",
            "cpu  120 0 120 860 0 0 0 0 120 0"), 40.0)

    def test_refuse_invalid_or_moving_backwards_counters(self):
        for first, second in (
            ("cpu 100 0 0 100 0 0 0 0", "cpu 100 0 0 100 0 0 0 0"),
            ("cpu 100 0 0 100 0 0 0 0", "cpu 90 0 0 90 0 0 0 0"),
            ("intr 1 2 3 4 5 6 7 8", "cpu 10 10 10 10 10 10 10 10"),
        ):
            with self.subTest(first=first):
                with self.assertRaises(ValueError):
                    AUDIT._cpu_busy(first, second)

    def test_private_markers_are_bounded_and_do_not_read_file(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            secret = base / "secret.json"
            secret.write_text("secret-do-not-read", encoding="utf-8")
            secret.chmod(0o600)
            marker = AUDIT._fixed_file_status(secret, directory=False, owner_uid=os.getuid())
            self.assertEqual(marker, "private_existing")
            self.assertNotIn("secret", marker)
            secret.chmod(0o644)
            self.assertEqual(AUDIT._fixed_file_status(
                secret, directory=False, owner_uid=os.getuid()), "unsafe_permissions")

    def test_private_markers_refuse_missing_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            self.assertEqual(AUDIT._fixed_file_status(
                base / "missing", directory=False, owner_uid=os.getuid()), "missing")
            (base / "target").write_text("x", encoding="utf-8")
            (base / "shortcut").symlink_to(base / "target")
            self.assertEqual(AUDIT._fixed_file_status(
                base / "shortcut", directory=False, owner_uid=os.getuid()), "symlink_refused")

    def test_present_directories_are_never_mutated(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root) / "private"
            base.mkdir(mode=0o700)
            original = base.stat()
            self.assertEqual(AUDIT._fixed_file_status(
                base, directory=True, owner_uid=os.getuid()), "private_existing")
            current = base.stat()
            self.assertEqual((current.st_mode, current.st_mtime_ns),
                             (original.st_mode, original.st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
