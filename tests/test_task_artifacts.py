"""6.12 offline: real GPT/Claude child files -> SHA -> private core SQLite.

Only disposable local workers; never invoke subscription CLIs, paid APIs, or host.
"""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

if sys.platform.startswith("linux"):
    from ai_agent_engine.task_api import (
        OwnerScope, TaskInbox, TaskNotFound, parse_submission,
    )
    from ai_agent_engine.task_assignment import WorkspaceRoot
    from ai_agent_engine.task_artifacts import (
        ArtifactTransferRejected, TaskArtifactTransfer,
        MAX_TRANSFER_FILE_BYTES,
    )
    from ai_agent_engine.task_claude import ClaudeTaskExecutor
    from ai_agent_engine.task_gpt import GPTTaskExecutor
    from ai_agent_engine.task_permissions import (
        ActionGrant, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_response import ArtifactDescriptor
    from ai_agent_engine.task_state import TaskState
    from ai_agent_engine.task_storage import SQLiteTaskStore

    OWNER = OwnerScope("owner-612", frozenset({"project-a", "project-b"}))
    FOREIGN = OwnerScope("other-owner", frozenset({"project-a"}))

WORKER = (
    "import json,sys,pathlib;"
    "request=json.loads(sys.stdin.buffer.read());"
    "pathlib.Path('nested').mkdir(mode=0o700,exist_ok=True);"
    "pathlib.Path('report.bin').write_bytes(bytes([0,68,65,84,65,10]));"
    "pathlib.Path('nested/summary.txt').write_text(request['model_id'],encoding='utf-8');"
    "print(json.dumps({'ok':True,'text':'report:'+request['model_id']}),flush=True)"
)


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                     "private Linux workspace and pidfd required")
class ArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-612-")
        self.dir = Path(self.temp.name)
        root = self.dir / "workspaces"
        root.mkdir(mode=0o700)
        for name in ("gpt", "claude", "other"):
            (root / name).mkdir(mode=0o700)
        self.root = WorkspaceRoot(root)
        self.db = self.dir / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.transfer = TaskArtifactTransfer(self.inbox, OWNER)
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.temp.cleanup()

    def task(self, project="project-a", *, scope=OWNER):
        return self.inbox.accept(parse_submission(json.dumps({
            "project_id": project, "title": "Offline binary report",
            "prompt": "FIXTURE ONLY",
        }).encode()), scope).task_id

    def run_worker(self, kind="gpt", *, slot=None, project="project-a"):
        slot = slot or kind
        tid = self.task(project)
        worker_id = kind + "-worker"
        gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(tid, worker_id, ProtectedAction.EXTERNAL_REQUEST),))
        args = (sys.executable, "-u", "-c", WORKER)
        if kind == "gpt":
            runner = GPTTaskExecutor(
                self.inbox, OWNER, worker_id=worker_id,
                model_id="gpt-612", permission_gate=gate,
                timeout_seconds=8, worker_argv=args)
        else:
            runner = ClaudeTaskExecutor(
                self.inbox, OWNER, authorized_owner_id=OWNER.owner_id,
                worker_id=worker_id, model_id="claude-612",
                permission_gate=gate, timeout_seconds=8, worker_argv=args)
        self.runners.append(runner)
        outcome = runner.execute(tid, slot)
        self.assertIs(outcome.state, TaskState.SUCCEEDED)
        self.assertEqual(runner.delivery.snapshot(tid, OWNER).status, "completed")
        return tid, runner, outcome, self.dir / "workspaces" / slot

    @staticmethod
    def descriptor(path, data, mime="application/octet-stream"):
        return ArtifactDescriptor(path, hashlib.sha256(data).hexdigest(), len(data), mime)

    def manifest(self, root):
        return (
            self.descriptor("report.bin", (root / "report.bin").read_bytes()),
            self.descriptor("nested/summary.txt",
                            (root / "nested/summary.txt").read_bytes(), "text/plain"),
        )

    def assert_code(self, expected, operation):
        with self.assertRaises(ArtifactTransferRejected) as caught:
            operation()
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected)

    def test_both_provider_processes_deliver_real_bytes_and_report_after_restart(self):
        known = []
        for kind, project in (("gpt", "project-a"), ("claude", "project-b")):
            tid, runner, outcome, work = self.run_worker(kind, project=project)
            manifest = self.manifest(work)
            saved = self.transfer.ingest(runner, outcome, manifest)
            self.assertEqual(saved.response.provider, kind)
            self.assertEqual(saved.response.text, "report:" + kind + "-612")
            self.assertEqual(saved.response.artifacts, manifest)
            self.assertEqual(saved.read_file("report.bin"), bytes([0,68,65,84,65,10]))
            self.assertEqual(saved.read_file("nested/summary.txt"),
                             (kind + "-612").encode())
            self.assertEqual(self.transfer.read_bundle(tid), saved)
            known.append((tid, saved))
            # Core-owned copy remains even when the original workspace file goes away.
            (work / "report.bin").unlink()
            self.assertEqual(self.transfer.read_bundle(tid).read_file("report.bin"),
                             bytes([0,68,65,84,65,10]))

        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.transfer = TaskArtifactTransfer(self.inbox, OWNER)
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (9,))
        for tid, expected in known:
            self.assertEqual(self.transfer.read_bundle(tid), expected)
            self.assertIs(self.inbox.state(tid, OWNER), TaskState.SUCCEEDED)
        self.assertEqual(len(self.inbox._store._db().execute(
            "SELECT * FROM task_artifact_blobs").fetchall()), 4)

    def test_report_without_files_is_still_a_durable_bundle(self):
        tid, runner, outcome, _ = self.run_worker()
        result = self.transfer.ingest(runner, outcome)
        self.assertEqual(result.files, ())
        self.assertEqual(self.transfer.read_bundle(tid).response.text,
                         "report:gpt-612")
        self.assert_code("already_transferred",
                         lambda: self.transfer.ingest(runner, outcome))

    def test_declared_checksum_and_size_are_checked_against_real_bytes(self):
        tid, runner, outcome, root = self.run_worker()
        good = self.manifest(root)[0]
        self.assert_code("checksum_mismatch", lambda: self.transfer.ingest(
            runner, outcome, (ArtifactDescriptor(
                good.path, "0" * 64, good.size_bytes, good.media_type),)))
        self.assert_code("size_mismatch", lambda: self.transfer.ingest(
            runner, outcome, (ArtifactDescriptor(
                good.path, good.sha256, good.size_bytes + 1, good.media_type),)))
        self.assert_code("size_limit", lambda: self.transfer.ingest(
            runner, outcome, (ArtifactDescriptor(
                good.path, good.sha256, MAX_TRANSFER_FILE_BYTES + 1,
                good.media_type),)))
        self.assert_code("not_found", lambda: self.transfer.read_bundle(tid))
        self.assertEqual(self.transfer.ingest(runner, outcome, (good,)).files,
                         ((good.path, (root / good.path).read_bytes()),))

    def test_invalid_manifest_paths_duplicates_and_unknown_types_are_denied(self):
        _, runner, outcome, root = self.run_worker()
        item = self.manifest(root)[0]
        for path in ("../outside", "/etc/passwd", "nested/../report.bin",
                     "nested//report.bin", r"C:\\system", "bad|path"):
            with self.subTest(path=path):
                self.assert_code("invalid_manifest", lambda path=path:
                    self.transfer.ingest(runner, outcome, (
                        ArtifactDescriptor(path, item.sha256, item.size_bytes,
                                           item.media_type),)))
        self.assert_code("invalid_manifest",
                         lambda: self.transfer.ingest(runner, outcome, (item, item)))
        self.assert_code("invalid_manifest", lambda: self.transfer.ingest(
            runner, outcome, (ArtifactDescriptor(
                item.path, item.sha256, item.size_bytes, "not a mime"),)))

    def test_symlink_hardlink_fifo_and_nested_symlink_are_denied(self):
        _, runner, outcome, root = self.run_worker()
        outside = self.dir / "private"
        outside.write_bytes(b"private fixture")
        (root / "sym").symlink_to(outside)
        (root / "hard").hardlink_to(root / "report.bin")
        (root / "sym-dir").symlink_to(self.dir)
        os.mkfifo(root / "pipe")
        data = b"private fixture"
        for path, payload, reason in (
            ("sym", data, "unsafe_path"),
            ("hard", bytes([0,68,65,84,65,10]), "unsafe_file"),
            ("pipe", b"", "unsafe_file"),
            ("sym-dir/private", data, "unsafe_path"),
        ):
            with self.subTest(path=path):
                self.assert_code(reason, lambda path=path, payload=payload:
                    self.transfer.ingest(runner, outcome,
                        (self.descriptor(path, payload),)))

    def test_busy_workspace_cannot_be_read_as_previous_workers_files(self):
        tid, runner, outcome, root = self.run_worker()
        newer = self.task("project-b")
        self.inbox.claim_task(newer, "next-worker", "gpt", OWNER)
        (root / "report.bin").write_bytes(b"OVERRIDDEN")
        self.assert_code("workspace_busy", lambda: self.transfer.ingest(
            runner, outcome, (self.manifest(root)[0],)))
        self.assert_code("not_found", lambda: self.transfer.read_bundle(tid))
        self.assertIs(self.inbox.state(newer, OWNER), TaskState.ASSIGNED)

    def test_storage_error_rolls_back_report_and_all_files_as_one_transaction(self):
        tid, runner, outcome, root = self.run_worker()
        db = self.inbox._store._db()
        db.execute("""CREATE TRIGGER abort_blob BEFORE INSERT ON task_artifact_blobs
            BEGIN SELECT RAISE(ABORT,'injected'); END""")
        manifest = self.manifest(root)
        try:
            self.assert_code("storage_unavailable", lambda: self.transfer.ingest(
                runner, outcome, manifest))
        finally:
            db.execute("DROP TRIGGER abort_blob")
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM task_artifact_reports WHERE task_id=?",
            (tid,)).fetchone(), (0,))
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM task_artifact_blobs WHERE task_id=?",
            (tid,)).fetchone(), (0,))
        self.assertEqual(len(self.transfer.ingest(runner, outcome, manifest).files), 2)

    def test_corrupt_stored_bytes_fail_instead_of_faking_valid_report(self):
        tid, runner, outcome, root = self.run_worker()
        self.transfer.ingest(runner, outcome, self.manifest(root))
        with self.inbox._store._db() as db:
            db.execute("""UPDATE task_artifact_blobs SET body=?
                WHERE task_id=? AND path='report.bin'""",
                (b"BADBYT", tid))
        self.assert_code("artifact_corrupt", lambda: self.transfer.read_bundle(tid))

    def test_foreign_owner_and_forged_outcome_are_rejected(self):
        tid, runner, outcome, root = self.run_worker()
        fake = type(outcome)(tid, TaskState.FAILED, outcome.model_id, True)
        self.assert_code("invalid_manifest", lambda: self.transfer.ingest(
            runner, fake, self.manifest(root)))
        self.assert_code("invalid_source", lambda: TaskArtifactTransfer(
            self.inbox, FOREIGN).ingest(runner, outcome))
        with self.assertRaises(TaskNotFound):
            TaskArtifactTransfer(self.inbox, FOREIGN).read_bundle(tid)
        self.assert_code("not_found", lambda: self.transfer.read_bundle(tid))

    def test_migration_from_private_schema_v8_preserves_prior_task(self):
        tid = self.task()
        self.inbox.close()
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE task_artifact_blobs")
            db.execute("DROP TABLE task_artifact_reports")
            db.execute("PRAGMA user_version=8")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.transfer = TaskArtifactTransfer(self.inbox, OWNER)
        self.assertIs(self.inbox.state(tid, OWNER), TaskState.QUEUED)
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (9,))
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT COUNT(*) FROM task_artifact_reports").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
