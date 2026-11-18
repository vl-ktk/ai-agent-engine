"""4.20: disposable create/verify/restore of nested project files.

Uses only a unique temporary tree. Never opens live Netcup data or backups.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest

from ai_agent_engine.local_backup import DB_NAME, LocalBackup


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(
    os.name == "posix" and shutil.which("cp") and shutil.which("tar"),
    "Requires POSIX, cp and tar for the documented restore procedure",
)
class NestedFileRestoreTests(unittest.TestCase):
    def test_isolated_snapshot_restores_nested_files_and_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="ae-restore-420-") as temp:
            root = Path(temp)
            data, backups, restored = (root / "source", root / "backups", root / "restored")
            data.mkdir(mode=0o700)
            backups.mkdir(mode=0o700)
            for name in ("projects", "workspaces", "artifacts"):
                (data / name).mkdir(mode=0o700)

            original_files = {
                "projects/demo/docs/README.txt": "Project documentation\n".encode(),
                "workspaces/demo/session/вход.txt": "Тестовые данные\n".encode(),
                "artifacts/demo/reports/result.bin": bytes(range(256)),
                "artifacts/demo/reports/empty.txt": b"",
            }
            for relative, content in original_files.items():
                path = data / relative
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes(content)
                path.chmod(0o600)
            empty_folder = data / "workspaces" / "demo" / "unused"
            empty_folder.mkdir(mode=0o700)

            live_db = data / "projects" / DB_NAME
            with sqlite3.connect(live_db) as db:
                db.execute("PRAGMA user_version=2")
                db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO items(value) VALUES ('synthetic fixture')")
            live_db.chmod(0o600)

            before = {name: digest(data / name) for name in original_files}
            before_db = digest(live_db)
            backup = LocalBackup(data, backups)
            snapshot_name, size, retained = backup.create()
            snapshot = backups / snapshot_name
            self.assertEqual(retained, 1)
            self.assertGreater(size, 0)
            self.assertEqual(backup.verify(snapshot), size)
            manifest = json.loads((snapshot / "manifest.json").read_text())
            self.assertEqual(manifest["file_count"], len(original_files))

            with tarfile.open(snapshot / "files.tar.gz", "r:gz") as archive:
                members = archive.getnames()
            for name in original_files:
                self.assertIn(name, members)
            self.assertIn("workspaces/demo/unused", members)
            self.assertNotIn("projects/engine.sqlite3", members)

            # Restore into a brand-new directory. No existing path is overwritten.
            restored.mkdir(mode=0o700)
            subprocess.run(
                ["cp", "--no-clobber", str(snapshot / DB_NAME), str(restored / DB_NAME)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["tar", "-xzf", str(snapshot / "files.tar.gz"), "-C", str(restored),
                 "--no-same-owner"],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["tar", "-dzf", str(snapshot / "files.tar.gz"), "-C", str(restored)],
                check=True, capture_output=True, text=True,
            )

            self.assertEqual(digest(snapshot / DB_NAME), digest(restored / DB_NAME))
            self.assertEqual(restored.stat().st_mode & 0o777, 0o700)
            self.assertEqual((restored / DB_NAME).stat().st_mode & 0o777, 0o600)
            for name in original_files:
                file = restored / name
                self.assertEqual(digest(file), before[name])
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(digest(data / name), before[name])
            self.assertTrue((restored / "workspaces" / "demo" / "unused").is_dir())
            for directory in (
                "projects/demo/docs", "workspaces/demo/session", "workspaces/demo/unused",
                "artifacts/demo/reports",
            ):
                self.assertEqual((restored / directory).stat().st_mode & 0o777, 0o700)

            with sqlite3.connect(restored / DB_NAME) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (2,))
                self.assertEqual(db.execute("SELECT value FROM items").fetchone(),
                                 ("synthetic fixture",))
            self.assertEqual(digest(live_db), before_db)


if __name__ == "__main__":
    unittest.main()
