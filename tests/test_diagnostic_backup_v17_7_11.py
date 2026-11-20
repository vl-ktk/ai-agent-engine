"""Disposable real backup/restore matrix for the forward-compatible hook."""

import os
import sqlite3
import unittest

if os.name != "posix":
    raise unittest.SkipTest("Linux private backup model")

from ai_agent_engine.diagnostic_backup_v17 import (
    DiagnosticBackup, DiagnosticBackupError, DATABASES,
)


class ForwardDiagnosticBackupTests(unittest.TestCase):
    def setUp(self):
        from tests.test_diagnostic_backup_7_7 import DiagnosticBackupTests
        self.fx = DiagnosticBackupTests("test_complete_snapshot_and_disposable_restore_preserve_live_databases")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.approve()
        self.store = DiagnosticBackup(self.fx.live, self.fx.backups, max_bytes=16 * 1024 * 1024)

    def test_snapshot_and_scratch_restore_accept_v14_to_v17(self):
        self.assertEqual(DATABASES["tasks.sqlite3"][1], (14, 15, 16, 17))
        task_db = self.fx.live / "tasks.sqlite3"
        original = task_db.read_bytes()
        for version in (14, 15, 16, 17):
            with self.subTest(version=version):
                with sqlite3.connect(task_db) as db:
                    db.execute(f"PRAGMA user_version={version}")
                before = task_db.read_bytes()
                name, _ = self.store.create()
                snapshot = self.fx.backups / name
                self.assertEqual(self.store.verify(snapshot)["scope"], "vl-ktk/ai-agent-engine")
                self.assertEqual(self.store.restore_smoke(snapshot)["status"], "RESTORE_SMOKE_PASS")
                self.assertEqual(task_db.read_bytes(), before)
        self.assertTrue(original)

    def test_unknown_schema_is_rejected_before_backup_creation(self):
        task_db = self.fx.live / "tasks.sqlite3"
        with sqlite3.connect(task_db) as db:
            db.execute("PRAGMA user_version=18")
        with self.assertRaises(DiagnosticBackupError):
            self.store.create()
        self.assertEqual(self.store._snapshots(), [])

    def test_old_v14_snapshot_remains_verified_after_v17_upgrade(self):
        from ai_agent_engine.diagnostic_backup import DiagnosticBackup as Original
        task_db = self.fx.live / "tasks.sqlite3"
        with sqlite3.connect(task_db) as db:
            db.execute("PRAGMA user_version=14")
        previous = Original(self.fx.live, self.fx.backups, max_bytes=16 * 1024 * 1024)
        name, _ = previous.create()
        with sqlite3.connect(task_db) as db:
            db.execute("PRAGMA user_version=17")
        self.assertEqual(self.store.verify(self.fx.backups / name)["scope"], "vl-ktk/ai-agent-engine")
        self.assertEqual(self.store.restore_smoke(self.fx.backups / name)["status"], "RESTORE_SMOKE_PASS")


if __name__ == "__main__":
    unittest.main()
