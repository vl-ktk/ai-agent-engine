"""6.13: real local Git worktrees + offline GPT/Claude task source receipts."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if sys.platform.startswith("linux"):
    from ai_agent_engine.task_api import (
        OwnerScope, TaskInbox, TaskNotFound, parse_submission,
    )
    from ai_agent_engine.task_assignment import WorkspaceRoot
    from ai_agent_engine.task_gpt import GPTTaskExecutor
    from ai_agent_engine.task_claude import ClaudeTaskExecutor
    from ai_agent_engine.task_permissions import (
        ActionGrant, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_state import TaskState
    from ai_agent_engine.task_storage import SQLiteTaskStore
    from ai_agent_engine.task_versions import (
        SourceVersionRejected, TaskSourceVersionRecorder,
    )

    OWNER = OwnerScope("owner-613", frozenset({"project-a", "project-b"}))
    FOREIGN = OwnerScope("other-owner", frozenset({"project-a"}))

CHANGE_WORKER = (
    "import json,pathlib,sys;"
    "r=json.loads(sys.stdin.buffer.read());"
    "pathlib.Path('tracked.txt').write_text('worker:'+r['model_id']);"
    "pathlib.Path('generated').mkdir(exist_ok=True);"
    "pathlib.Path('generated/summary.txt').write_text('verified source');"
    "print(json.dumps({'ok':True,'text':'finished'}),flush=True)"
)
ECHO_WORKER = (
    "import json,sys;"
    "r=json.loads(sys.stdin.buffer.read());"
    "print(json.dumps({'ok':True,'text':'finished'}),flush=True)"
)
FAIL_WORKER = "import sys; sys.stdin.buffer.read(); raise SystemExit(1)"


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                     "requires Linux private task store and real child process")
class VersionRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-613-")
        self.directory = Path(self.temp.name)
        root = self.directory / "workspaces"
        root.mkdir(mode=0o700)
        self.workspaces = {}
        self.base_shas = {}
        for slot in ("gpt", "claude"):
            ws = root / slot
            ws.mkdir(mode=0o700)
            self.git(ws, "init", "-q")
            (ws / "tracked.txt").write_text("baseline")
            self.git(ws, "add", "--", "tracked.txt")
            self.git(ws, "-c", "user.name=Local Test", "-c",
                     "user.email=local@example.invalid", "commit", "-qm", "base")
            self.workspaces[slot] = ws
            self.base_shas[slot] = self.git(ws, "rev-parse", "HEAD").strip()
        self.root = WorkspaceRoot(root)
        self.db = self.directory / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.recorder = TaskSourceVersionRecorder(self.inbox, OWNER)
        self.runners = []

    @staticmethod
    def git(directory, *args):
        result = subprocess.run(["git", "-C", str(directory), *args],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, check=False, text=True)
        if result.returncode:
            raise AssertionError("disposable local Git test operation failed")
        return result.stdout

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.temp.cleanup()

    def run_worker(self, kind="gpt", *, command=CHANGE_WORKER):
        task = self.inbox.accept(parse_submission(json.dumps({
            "project_id": "project-a" if kind == "gpt" else "project-b",
            "title": "Local Git source identity",
            "prompt": "synthetic fixture only",
        }).encode()), OWNER).task_id
        worker = kind + "-worker"
        gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(task, worker, ProtectedAction.EXTERNAL_REQUEST),))
        kwargs = dict(worker_id=worker, model_id=kind+"-613",
                      permission_gate=gate, timeout_seconds=10,
                      worker_argv=(sys.executable, "-u", "-c", command))
        if kind == "claude":
            runner = ClaudeTaskExecutor(self.inbox, OWNER,
                                        authorized_owner_id=OWNER.owner_id, **kwargs)
        else:
            runner = GPTTaskExecutor(self.inbox, OWNER, **kwargs)
        self.runners.append(runner)
        outcome = runner.execute(task, kind)
        return task, runner, outcome

    def assert_code(self, code, fn):
        with self.assertRaises(SourceVersionRejected) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_both_real_provider_adapters_record_dirty_patch_and_reverify_after_restart(self):
        copies = []
        for kind in ("gpt", "claude"):
            tid, runner, outcome = self.run_worker(kind)
            self.assertIs(outcome.state, TaskState.SUCCEEDED)
            receipt = self.recorder.record(
                runner, outcome, baseline_commit=self.base_shas[kind])
            self.assertEqual(receipt.task_id, tid)
            self.assertEqual(receipt.worker_id, kind + "-worker")
            self.assertEqual(receipt.baseline_commit, self.base_shas[kind])
            self.assertEqual(receipt.result_commit, self.base_shas[kind])
            self.assertRegex(receipt.patch_sha256, r"^[0-9a-f]{64}$")
            self.assertEqual(self.recorder.read_version(tid), receipt)
            self.assertEqual(self.recorder.verify_version(tid), receipt)
            copies.append((tid, receipt))

        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.recorder = TaskSourceVersionRecorder(self.inbox, OWNER)
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (16,))
        for tid, receipt in copies:
            self.assertEqual(self.recorder.read_version(tid), receipt)
            self.assertEqual(self.recorder.verify_version(tid), receipt)

    def test_clean_repository_has_commit_id_without_fake_patch(self):
        tid, runner, outcome = self.run_worker(command=ECHO_WORKER)
        receipt = self.recorder.record(runner, outcome,
                                       baseline_commit=self.base_shas["gpt"])
        self.assertEqual(receipt.result_commit, self.base_shas["gpt"])
        self.assertIsNone(receipt.patch_sha256)
        self.assertEqual(self.recorder.verify_version(tid), receipt)

    def test_post_task_fast_forward_local_commit_is_allowed_and_pinned(self):
        tid, runner, outcome = self.run_worker()
        ws = self.workspaces["gpt"]
        self.git(ws, "add", "--", "tracked.txt", "generated/summary.txt")
        self.git(ws, "-c", "user.name=Local Test", "-c",
                 "user.email=local@example.invalid", "commit", "-qm", "result")
        after = self.git(ws, "rev-parse", "HEAD").strip()
        receipt = self.recorder.record(runner, outcome,
                                       baseline_commit=self.base_shas["gpt"])
        self.assertEqual(receipt.result_commit, after)
        self.assertNotEqual(receipt.baseline_commit, after)
        self.assertIsNone(receipt.patch_sha256)
        self.assertEqual(self.recorder.verify_version(tid), receipt)

    def test_unrelated_new_head_or_unknown_base_is_rejected(self):
        tid, runner, outcome = self.run_worker()
        self.assert_code("source_diverged", lambda: self.recorder.record(
            runner, outcome, baseline_commit="0" * 40))
        ws = self.workspaces["gpt"]
        self.git(ws, "checkout", "--orphan", "other")
        self.git(ws, "add", "-A")
        self.git(ws, "-c", "user.name=Local Test", "-c",
                 "user.email=local@example.invalid", "commit", "-qm", "unrelated")
        self.assert_code("source_diverged", lambda: self.recorder.record(
            runner, outcome, baseline_commit=self.base_shas["gpt"]))
        self.assert_code("not_found", lambda: self.recorder.read_version(tid))

    def test_edit_after_record_is_detected_by_explicit_recheck(self):
        tid, runner, outcome = self.run_worker()
        receipt = self.recorder.record(runner, outcome,
                                       baseline_commit=self.base_shas["gpt"])
        (self.workspaces["gpt"] / "tracked.txt").write_text("tampered")
        self.assert_code("source_changed", lambda: self.recorder.verify_version(tid))
        self.assertEqual(self.recorder.read_version(tid), receipt)

    def test_untracked_content_and_path_are_in_patch_digest(self):
        tid, runner, outcome = self.run_worker(command=ECHO_WORKER)
        ws = self.workspaces["gpt"]
        new = ws / "new.txt"
        new.write_text("a")
        receipt = self.recorder.record(runner, outcome,
                                       baseline_commit=self.base_shas["gpt"])
        self.assertRegex(receipt.patch_sha256, r"^[0-9a-f]{64}$")
        new.write_text("b")
        self.assert_code("source_changed", lambda: self.recorder.verify_version(tid))
        new.write_text("a")
        self.assertEqual(self.recorder.verify_version(tid), receipt)
        new.rename(ws / "other.txt")
        self.assert_code("source_changed", lambda: self.recorder.verify_version(tid))

    def test_untracked_symlink_and_giant_source_are_denied(self):
        tid, runner, outcome = self.run_worker(command=ECHO_WORKER)
        ws = self.workspaces["gpt"]
        (ws / "bad").symlink_to(self.directory / "private")
        self.assert_code("unsafe_source", lambda: self.recorder.record(
            runner, outcome, baseline_commit=self.base_shas["gpt"]))
        (ws / "bad").unlink()
        (ws / "giant").write_bytes(b"A" * 8_388_609)
        self.assert_code("source_too_large", lambda: self.recorder.record(
            runner, outcome, baseline_commit=self.base_shas["gpt"]))

    def test_workspace_reassigned_after_success_cannot_be_copied(self):
        tid, runner, outcome = self.run_worker()
        next_id = self.inbox.accept(parse_submission(json.dumps({
            "project_id": "project-b", "title": "Next", "prompt": "second",
        }).encode()), OWNER).task_id
        self.inbox.claim_task(next_id, "next-worker", "gpt", OWNER)
        self.assert_code("workspace_busy", lambda: self.recorder.record(
            runner, outcome, baseline_commit=self.base_shas["gpt"]))
        self.assert_code("not_found", lambda: self.recorder.read_version(tid))

    def test_foreign_scope_and_forged_or_failed_provider_outcome_are_rejected(self):
        tid, runner, outcome = self.run_worker()
        other = TaskSourceVersionRecorder(self.inbox, FOREIGN)
        self.assert_code("invalid_source", lambda: other.record(
            runner, outcome, baseline_commit=self.base_shas["gpt"]))
        with self.assertRaises(TaskNotFound):
            other.read_version(tid)
        fake = type(outcome)(tid, TaskState.FAILED, outcome.model_id, True)
        self.assert_code("invalid_source", lambda: self.recorder.record(
            runner, fake, baseline_commit=self.base_shas["gpt"]))
        self.assert_code("invalid_source", lambda: self.recorder.record(
            runner, outcome, baseline_commit="invalid"))
        self.assert_code("not_found", lambda: self.recorder.read_version(tid))

    def test_duplicate_receipt_cannot_rewrite_committed_metadata(self):
        tid, runner, outcome = self.run_worker()
        first = self.recorder.record(runner, outcome,
                                     baseline_commit=self.base_shas["gpt"])
        self.assert_code("already_recorded", lambda: self.recorder.record(
            runner, outcome, baseline_commit=self.base_shas["gpt"]))
        self.assertEqual(self.recorder.read_version(tid), first)

    def test_database_write_failure_does_not_fabricate_receipt(self):
        tid, runner, outcome = self.run_worker()
        db = self.inbox._store._db()
        db.execute("""CREATE TRIGGER abort_version BEFORE INSERT ON task_source_versions
            BEGIN SELECT RAISE(ABORT,'injected'); END""")
        try:
            self.assert_code("storage_unavailable", lambda: self.recorder.record(
                runner, outcome, baseline_commit=self.base_shas["gpt"]))
        finally:
            db.execute("DROP TRIGGER abort_version")
        self.assert_code("not_found", lambda: self.recorder.read_version(tid))
        self.assertEqual(self.recorder.record(
            runner, outcome, baseline_commit=self.base_shas["gpt"]).task_id, tid)

    def test_recheck_race_refuses_without_recording_false_version(self):
        from ai_agent_engine import task_versions
        tid, runner, outcome = self.run_worker()
        original = task_versions._snapshot
        calls = [0]
        def change_between_snapshots(path, fd):
            item = original(path, fd)
            calls[0] += 1
            if calls[0] == 1:
                (self.workspaces["gpt"] / "tracked.txt").write_text("later")
            return item
        with patch.object(task_versions, "_snapshot",
                          side_effect=change_between_snapshots):
            self.assert_code("source_changed", lambda: self.recorder.record(
                runner, outcome, baseline_commit=self.base_shas["gpt"]))
        self.assert_code("not_found", lambda: self.recorder.read_version(tid))

    def test_source_receipt_tampering_is_detected(self):
        tid, runner, outcome = self.run_worker()
        self.recorder.record(runner, outcome,
                             baseline_commit=self.base_shas["gpt"])
        with self.inbox._store._db() as db:
            db.execute("UPDATE task_source_versions SET result_commit=? WHERE task_id=?",
                       ("Z" * 40, tid))
        self.assert_code("version_corrupt", lambda: self.recorder.read_version(tid))

    def test_migration_of_private_v9_retains_original_task(self):
        tid = self.inbox.accept(parse_submission(json.dumps({
            "project_id": "project-a", "title": "Persist",
            "prompt": "Old v9 record",
        }).encode()), OWNER).task_id
        self.inbox.close()
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE task_source_versions")
            db.execute("PRAGMA user_version=9")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.recorder = TaskSourceVersionRecorder(self.inbox, OWNER)
        self.assertIs(self.inbox.state(tid, OWNER), TaskState.QUEUED)
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (16,))


if __name__ == "__main__":
    unittest.main()
