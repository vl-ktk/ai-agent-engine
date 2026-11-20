"""Offline fail-closed checks for the Netcup backup hook metadata inventory."""

import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


if os.name != "posix":
    raise unittest.SkipTest("Netcup owner-admin metadata audit is POSIX-only")

SPEC = importlib.util.spec_from_file_location(
    "audit_backup_hook_metadata_7_11",
    Path(__file__).resolve().parents[1]
    / "ops/admin-scripts/audit_backup_hook_metadata_7_11.py",
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class HookMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hook.py"
        self.path.write_bytes(b"reviewed diagnostic backup hook\n")
        self.path.chmod(0o755)
        self.expected = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def audit(self):
        return AUDIT._metadata(self.path, uid=os.getuid(), expected_hash=self.expected)

    def test_exact_reviewed_regular_file(self):
        result = self.audit()
        self.assertEqual(result["diagnosis"], "reviewed_hook_match")
        self.assertEqual(result["sha256_check"], "match")
        self.assertTrue(result["mode_expected"])

    def test_hash_drift_not_executed_or_disclosed(self):
        self.path.write_bytes(b"unexpected script bytes\n")
        result = self.audit()
        self.assertEqual(result["diagnosis"], "hook_hash_mismatch")
        self.assertEqual(result["sha256_check"], "mismatch")
        self.assertNotIn("unexpected script", repr(result))

    def test_missing_is_classified(self):
        self.path.unlink()
        self.assertEqual(self.audit()["diagnosis"], "missing")

    def test_symlink_is_not_followed(self):
        target = self.path.with_name("target.py")
        self.path.rename(target)
        self.path.symlink_to(target)
        result = self.audit()
        self.assertEqual(result["diagnosis"], "symlink_refused")
        self.assertEqual(result["sha256_check"], "not_checked")

    def test_directory_is_refused(self):
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.audit()["diagnosis"], "wrong_type")

    def test_wrong_owner_is_classified_without_hash(self):
        with self.subTest("different expected UID"):
            result = AUDIT._metadata(self.path, uid=os.getuid() + 10000,
                                     expected_hash=self.expected)
            self.assertEqual(result["diagnosis"], "wrong_owner")
            self.assertEqual(result["sha256_check"], "not_checked")

    def test_wrong_mode_is_classified_without_hash(self):
        self.path.chmod(0o700)
        result = self.audit()
        self.assertEqual(result["diagnosis"], "wrong_mode")
        self.assertEqual(result["sha256_check"], "not_checked")

    def test_hardlink_is_classified_without_hash(self):
        second = self.path.with_name("other-link.py")
        os.link(self.path, second)
        result = self.audit()
        self.assertEqual(result["diagnosis"], "unexpected_hardlinks")
        self.assertEqual(result["sha256_check"], "not_checked")


if __name__ == "__main__":
    unittest.main()
