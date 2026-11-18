"""Isolated backup/rotation tests; never touch live system paths."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from ai_agent_engine.local_backup import BackupError, LocalBackup, DB_NAME


@unittest.skipUnless(os.name == "posix", "Linux local backup security model")
class LocalBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-backup-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.backups = root / "backup"
        self.data.mkdir(mode=0o700)
        self.backups.mkdir(mode=0o700)
        for name in ("projects", "workspaces", "artifacts"):
            (self.data / name).mkdir(mode=0o700)
        self.db = self.data / "projects" / DB_NAME
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO items(value) VALUES (?)", ("valuable user data",))
        self.db.chmod(0o600)
        (self.data / "projects" / "metadata.txt").write_text("project metadata")
        (self.data / "workspaces" / "input.txt").write_text("workspace input")
        (self.data / "artifacts" / "result.txt").write_text("artifact result")
        self.store = LocalBackup(self.data, self.backups)

    def test_creates_verifiable_snapshot_without_changing_source(self):
        before = self.db.read_bytes()
        name, size, count = self.store.create()
        self.assertEqual(count, 1)
        self.assertGreater(size, 100)
        self.assertEqual(self.store.verify(self.backups / name), size)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.store.newest(), (1, size))
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()),
                         [".backup.lock", name])

    def test_archive_contains_files_but_not_live_database(self):
        name, _, _ = self.store.create()
        with tarfile.open(self.backups / name / "files.tar.gz", "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("projects/metadata.txt", names)
        self.assertIn("workspaces/input.txt", names)
        self.assertIn("artifacts/result.txt", names)
        self.assertNotIn("projects/engine.sqlite3", names)

    def test_created_files_are_private(self):
        name, _, _ = self.store.create()
        snapshot = self.backups / name
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        for file in snapshot.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_tampered_database_is_rejected(self):
        name, _, _ = self.store.create()
        with (self.backups / name / DB_NAME).open("ab") as stream:
            stream.write(b"corruption")
        with self.assertRaises(BackupError):
            self.store.verify(self.backups / name)

    def test_tampered_archive_is_rejected(self):
        name, _, _ = self.store.create()
        with (self.backups / name / "files.tar.gz").open("ab") as stream:
            stream.write(b"corruption")
        with self.assertRaises(BackupError):
            self.store.verify(self.backups / name)

    def test_manifest_mismatch_is_rejected(self):
        name, _, _ = self.store.create()
        path = self.backups / name / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["file_count"] += 1
        path.write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            self.store.verify(self.backups / name)

    def test_keeps_only_last_two_by_rotation(self):
        self.store = LocalBackup(self.data, self.backups, keep=2)
        names = [self.store.create()[0] for _ in range(3)]
        self.assertNotEqual(names[0], names[1])
        self.assertFalse((self.backups / names[0]).exists())
        self.assertTrue((self.backups / names[1]).exists())
        self.assertTrue((self.backups / names[2]).exists())
        self.assertEqual(self.store.newest()[0], 2)

    def test_budget_rotation_removes_only_older_verified_snapshot(self):
        first, first_size, _ = self.store.create()
        self.store = LocalBackup(self.data, self.backups, max_bytes=first_size + 512)
        second, _, retained = self.store.create()
        self.assertEqual(retained, 1)
        self.assertFalse((self.backups / first).exists())
        self.assertTrue((self.backups / second).exists())
        self.assertTrue(self.db.exists())

    def test_live_wal_is_reflected_in_consistent_database_snapshot(self):
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("INSERT INTO items(value) VALUES ('wal entry')")
            connection.commit()
            name, _, _ = self.store.create()
            with sqlite3.connect(self.backups / name / DB_NAME) as snapshot:
                values = [row[0] for row in snapshot.execute("SELECT value FROM items")]
            self.assertEqual(values, ["valuable user data", "wal entry"])
            with tarfile.open(self.backups / name / "files.tar.gz", "r:gz") as tar:
                self.assertFalse(any("engine.sqlite3" in n for n in tar.getnames()))
        finally:
            connection.close()

    def test_small_budget_preserves_source_and_existing_backups(self):
        first = self.store.create()[0]
        self.store = LocalBackup(self.data, self.backups, max_bytes=1024)
        with self.assertRaises(BackupError):
            self.store.create()
        self.assertTrue((self.backups / first).exists())
        self.assertTrue(self.db.exists())

    def test_missing_database_fails_without_publishing(self):
        self.db.rename(self.data / "projects" / "database_elsewhere")
        with self.assertRaises(BackupError):
            self.store.create()
        self.assertEqual(self.store._snapshots(), [])

    def test_symlink_inside_data_fails(self):
        (self.data / "workspaces" / "link").symlink_to(self.db)
        with self.assertRaises(BackupError):
            self.store.create()
        self.assertEqual(self.store._snapshots(), [])

    def test_fifo_inside_data_fails(self):
        os.mkfifo(self.data / "artifacts" / "pipe")
        with self.assertRaises(BackupError):
            self.store.create()

    def test_wrong_backup_dir_mode_fails(self):
        self.backups.chmod(0o755)
        with self.assertRaises(BackupError):
            self.store.create()

    def test_symlink_backup_root_fails(self):
        destination = self.backups.with_name("real-backup")
        self.backups.rename(destination)
        self.backups.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(BackupError):
            self.store.create()

    def test_no_backup_reports_error(self):
        with self.assertRaises(BackupError):
            self.store.newest()

    def test_invalid_snapshot_reference_rejected(self):
        name, _, _ = self.store.create()
        with self.assertRaises(BackupError):
            self.store.verify(self.data / name)

    def test_no_output_exposes_source_file_contents(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.store.create()
        self.assertNotIn("valuable user data", output.getvalue())


if __name__ == "__main__":
    unittest.main()
