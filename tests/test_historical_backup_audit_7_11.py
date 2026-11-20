"""Read-only historical Netcup hook attestation tests."""

import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


if os.name != "posix":
    raise unittest.SkipTest("Netcup backup audit is POSIX-only")

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "historical_backup_7_11",
    ROOT / "ops/admin-scripts/audit_historical_backup_7_11.py",
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class HistoricalBackupAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "hook.py"
        self.file.write_bytes(b"historical vetted backup\n")
        self.file.chmod(0o755)

    def test_known_digest_is_accepted(self):
        digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        with patch.dict(AUDIT.KNOWN, {digest: ("known", (14,))}):
            with patch.object(AUDIT, "_private_regular", return_value=True):
                self.assertEqual(AUDIT._known_version(self.file), ("known", (14,)))

    def test_unreviewed_digest_refused(self):
        with patch.object(AUDIT, "_private_regular", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "not historically reviewed"):
                AUDIT._known_version(self.file)

    def test_wrong_mode_refused(self):
        self.file.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
            AUDIT._known_version(self.file)

    def test_symlink_refused(self):
        link = self.file.with_name("link.py")
        link.symlink_to(self.file)
        with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
            AUDIT._known_version(link)

    def test_app_id_and_integrity(self):
        dbpath = Path(self.temp.name) / "tasks.sqlite3"
        with sqlite3.connect(dbpath) as db:
            db.execute(f"PRAGMA application_id={0x4145494E}")
            db.execute("PRAGMA user_version=14")
        dbpath.chmod(0o600)
        with (patch.object(AUDIT, "TASKS", dbpath),
              patch.object(AUDIT, "_private_regular", return_value=True)):
            self.assertEqual(AUDIT._task_schema(os.getuid()), 14)

    def test_app_id_mismatch_refused(self):
        dbpath = Path(self.temp.name) / "tasks.sqlite3"
        with sqlite3.connect(dbpath) as db:
            db.execute("PRAGMA user_version=14")
        dbpath.chmod(0o600)
        with (patch.object(AUDIT, "TASKS", dbpath),
              patch.object(AUDIT, "_private_regular", return_value=True)):
            with self.assertRaisesRegex(RuntimeError, "identity/integrity"):
                AUDIT._task_schema(os.getuid())


if __name__ == "__main__":
    unittest.main()
