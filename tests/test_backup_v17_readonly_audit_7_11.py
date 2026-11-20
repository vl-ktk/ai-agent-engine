"""Offline checks for the immutable 7.11 Netcup backup audit."""

import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if os.name != "posix":
    raise unittest.SkipTest("Netcup owner-admin audit runs only on POSIX hosts")

SPEC = importlib.util.spec_from_file_location(
    "audit_backup_v17_readonly_7_11",
    ROOT / "ops/admin-scripts/audit_backup_v17_readonly_7_11.py",
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class BackupV17AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        with sqlite3.connect(self.path) as db:
            db.execute(f"PRAGMA application_id={0x4145494E}")
            db.execute("PRAGMA user_version=15")
        self.path.chmod(0o600)

    def _schema(self):
        with (patch.object(AUDIT, "TASKS", self.path),
              patch.object(AUDIT.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid()))):
            return AUDIT._schema()

    def test_correct_id_and_schema_are_reported(self):
        self.assertEqual(self._schema(), {
            "application_id_valid": True,
            "schema_version": 15,
            "quick_check": "PASS",
        })

    def test_unknown_application_id_rejected(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA application_id=0")
        with self.assertRaisesRegex(RuntimeError, "identity or integrity"):
            self._schema()

    def test_unexpected_permissions_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
            self._schema()

    def test_sidecar_rejected_without_opening_database(self):
        sidecar = Path(str(self.path) + "-wal")
        sidecar.touch()
        with self.assertRaisesRegex(RuntimeError, "sidecars"):
            self._schema()

    def test_blessed_hook_still_excludes_v17(self):
        from ai_agent_engine import diagnostic_backup
        self.assertEqual(diagnostic_backup.DATABASES["tasks.sqlite3"][1], (14, 15))
        self.assertEqual(
            AUDIT._hash(ROOT / "ai_agent_engine/diagnostic_backup.py"),
            AUDIT.BACKUP_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
