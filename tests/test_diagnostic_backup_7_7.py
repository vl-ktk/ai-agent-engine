"""Disposable 7.7 diagnostic backup and restore tests; never touch Netcup."""

from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest

from ai_agent_engine.diagnostic_backup import DiagnosticBackup, DiagnosticBackupError, DATABASES
from ai_agent_engine.main_orchestrator import OwnerDecision
from tests.test_owner_project_registration_7_7 import APPROVAL, PROVISION, ROOT


spec = importlib.util.spec_from_file_location(
    "enable_diagnostic_backup_7_7",
    ROOT / "ops/admin-scripts/enable_diagnostic_backup_7_7.py")
INSTALLER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(INSTALLER)


@unittest.skipUnless(os.name == "posix", "Linux private backup model")
class DiagnosticBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ae-diag-backup-77-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        origin = self.root / "origin"
        origin.mkdir(mode=0o700)
        self.git("init", "-q", str(origin))
        (origin / "README.md").write_text("private backup test data\n")
        self.git("-C", str(origin), "add", "README.md")
        self.git("-C", str(origin), "-c", "user.name=Offline", "-c",
                 "user.email=offline@example.invalid", "commit", "-qm", "initial")
        self.commit = self.git("-C", str(origin), "rev-parse", "HEAD")
        self.live = self.root / "live"
        evidence = self.root / "evidence"
        evidence.mkdir(mode=0o700)
        PROVISION.provision_empty_project(
            self.live, source_url=str(origin), commit=self.commit,
            uid=os.geteuid(), gid=os.getegid(), recovery=evidence)
        self.backups = self.root / "backups"
        self.backups.mkdir(mode=0o700)
        self.store = DiagnosticBackup(self.live, self.backups, max_bytes=16 * 1024 * 1024)

    @staticmethod
    def git(*args):
        return subprocess.run(("git", *args), capture_output=True,
                              check=True, text=True, timeout=15).stdout.strip()

    def approve(self):
        proposal = APPROVAL.load_pinned_proposal(ROOT)
        decision = OwnerDecision(proposal.owner_id, proposal.proposal_id,
                                 proposal.sha256, "button")
        APPROVAL.register_approved_project(
            self.live, proposal, decision, prepared_source_sha=self.commit)

    def test_complete_snapshot_and_disposable_restore_preserve_live_databases(self):
        self.approve()
        originals = {name: (self.live / name).read_bytes() for name in DATABASES}
        name, retained = self.store.create()
        self.assertEqual(retained, 1)
        snapshot = self.backups / name
        self.assertEqual(self.store.verify(snapshot)["scope"], "vl-ktk/ai-agent-engine")
        self.assertEqual(self.store.restore_smoke(snapshot)["status"], "RESTORE_SMOKE_PASS")
        self.assertEqual({name: (self.live / name).read_bytes() for name in DATABASES}, originals)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(f.stat().st_mode & 0o777 == 0o600
                            for f in snapshot.iterdir()))

    def test_archive_contains_workspace_and_config_not_live_sqlite(self):
        self.approve()
        name, _ = self.store.create()
        with tarfile.open(self.backups / name / "files.tar.gz", "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("runtime.json", names)
        self.assertIn("workspaces/ai-agent-engine/README.md", names)
        self.assertFalse(any(n.endswith(".sqlite3") for n in names))
        self.assertEqual(names.count("runtime.json"), 1)

    def test_unapproved_project_refused_without_new_snapshot(self):
        with self.assertRaises(DiagnosticBackupError):
            self.store.create()
        self.assertEqual(self.store._snapshots(), [])

    def test_unexpected_task_refused(self):
        self.approve()
        with sqlite3.connect(self.live / "tasks.sqlite3") as db:
            db.execute("INSERT INTO tasks "
                       "(task_id,receipt,owner_id,project_id,title,prompt,submitted_priority,priority) "
                       "VALUES (?,?,?,?,?,?,?,?)", (
                           "task", 1, "vl-ktk", "ai-agent-engine", "check", "test", 50, 50))
            db.execute("INSERT INTO task_states(task_id,state) VALUES (?,?)", (
                "task", "running"))
        with self.assertRaisesRegex(DiagnosticBackupError, "quiescent"):
            self.store.create()

    def test_completed_task_does_not_block_future_backups(self):
        self.approve()
        with sqlite3.connect(self.live / "tasks.sqlite3") as db:
            db.execute("INSERT INTO tasks "
                       "(task_id,receipt,owner_id,project_id,title,prompt,submitted_priority,priority) "
                       "VALUES (?,?,?,?,?,?,?,?)", (
                           "task", 1, "vl-ktk", "ai-agent-engine", "check", "test", 50, 50))
            db.execute("INSERT INTO task_states(task_id,state) VALUES (?,?)", (
                "task", "succeeded"))
        name, _ = self.store.create()
        self.assertEqual(self.store.verify(self.backups / name)["scope"],
                         "vl-ktk/ai-agent-engine")

    def test_symlink_and_fifo_in_source_refused(self):
        self.approve()
        bad = self.live / "workspaces/ai-agent-engine/fake"
        bad.symlink_to(self.live / "runtime.json")
        with self.assertRaises(DiagnosticBackupError):
            self.store.create()
        bad.unlink()
        os.mkfifo(bad)
        with self.assertRaises(DiagnosticBackupError):
            self.store.create()
        self.assertEqual(self.store._snapshots(), [])

    def test_tampered_snapshot_database_or_manifest_is_rejected(self):
        self.approve()
        name, _ = self.store.create()
        snapshot = self.backups / name
        target = snapshot / "tasks.sqlite3"
        with target.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(DiagnosticBackupError):
            self.store.verify(snapshot)

    def test_wrong_private_destination_permissions_refused(self):
        self.approve()
        self.backups.chmod(0o755)
        with self.assertRaises(DiagnosticBackupError):
            self.store.create()

    def test_verified_retention_only_affects_dedicated_snapshots(self):
        self.approve()
        self.store = DiagnosticBackup(self.live, self.backups, keep=2,
                                      max_bytes=16 * 1024 * 1024)
        survivor = self.backups / "KEEP_THIS.txt"
        survivor.write_text("unrelated private file")
        names = [self.store.create()[0] for _ in range(3)]
        self.assertFalse((self.backups / names[0]).exists())
        self.assertTrue((self.backups / names[1]).exists())
        self.assertTrue((self.backups / names[2]).exists())
        self.assertEqual(survivor.read_text(), "unrelated private file")

    def test_untrusted_extra_project_denied(self):
        self.approve()
        with sqlite3.connect(self.live / "projects/project-leaders.sqlite3") as db:
            db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,'active')", (
                "foreign", "foreign", "Foreign", "proposal_" + "f" * 32,
                "0" * 64, "button"))
        with self.assertRaises(DiagnosticBackupError):
            self.store.create()

    def test_systemd_extension_is_one_shot_and_preserves_existing_backup(self):
        dropin = INSTALLER.dropin_bytes().decode("ascii")
        self.assertIn("ExecStartPost=/usr/bin/python3 /usr/local/libexec/ae-diagnostic-backup.py create", dropin)
        self.assertIn("ReadOnlyPaths=/var/lib/ai-agent-engine-diagnostics", dropin)
        self.assertIn("ReadWritePaths=/var/backups/ai-agent-engine-diagnostics", dropin)
        self.assertNotIn("ExecStart=", dropin)
        self.assertNotIn("ExecStartPre=", dropin)
        self.assertEqual(INSTALLER._hash(ROOT / "ai_agent_engine/diagnostic_backup.py"),
                         INSTALLER.REVIEWED_BACKUP_SHA256)
        for path, digest in INSTALLER.BASE_HASHES.items():
            source = (ROOT / "infra/systemd" / path.name
                      if path.name.endswith((".service", ".timer"))
                      else ROOT / "ai_agent_engine/local_backup.py")
            self.assertEqual(INSTALLER._hash(source), digest)

    def test_corrupted_old_snapshot_stops_rotation_without_deletion(self):
        self.approve()
        self.store = DiagnosticBackup(self.live, self.backups, keep=1,
                                      max_bytes=16 * 1024 * 1024)
        first, _ = self.store.create()
        with (self.backups / first / "tasks.sqlite3").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(DiagnosticBackupError):
            self.store.create()
        self.assertTrue((self.backups / first).exists())


if __name__ == "__main__":
    unittest.main()
