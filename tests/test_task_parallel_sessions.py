"""6.10: two independent real Linux worker processes run concurrently.

GPT/Claude adapter orchestration is exercised with disposable, deterministic
subprocesses. This is NOT a live provider call or subscription authorization.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

if sys.platform.startswith("linux"):
    from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
    from ai_agent_engine.task_assignment import WorkspaceBusy, WorkspaceRoot
    from ai_agent_engine.task_claude import ClaudeTaskExecutor
    from ai_agent_engine.task_gpt import GPTTaskExecutor
    from ai_agent_engine.task_permissions import (
        ActionGrant, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_state import TaskState
    from ai_agent_engine.task_storage import SQLiteTaskStore
    OWNER = OwnerScope("owner-personal", frozenset({"project-a", "project-b"}))

# Executed as a REAL private process in each pre-existing task workspace.
# The parent waits for both markers while both workers are still running.
WAITING_WORKER = (
    "import json,pathlib,sys,time;"
    "request=json.loads(sys.stdin.buffer.read());"
    "pathlib.Path('started').write_text(request['model_id']);"
    "deadline=time.monotonic()+15;"
    "\nwhile not pathlib.Path('release').exists():"
    "\n if time.monotonic()>deadline: raise SystemExit(12)"
    "\n time.sleep(.01)"
    "\nprint(json.dumps({'ok':True,'text':request['model_id']+':'+request['prompt']}),flush=True)"
)


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                     "Linux pidfd and /proc are required for gated process identity")
class ParallelProviderSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-610-")
        self.base = Path(self.temp.name)
        workspaces = self.base / "workspaces"
        workspaces.mkdir(mode=0o700)
        (workspaces / "gpt-slot").mkdir(mode=0o700)
        (workspaces / "claude-slot").mkdir(mode=0o700)
        self.root = WorkspaceRoot(workspaces)
        self.db_path = self.base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db_path),
                               workspace_root=self.root)
        self.runners = []

    def tearDown(self):
        try:
            for runner in self.runners:
                runner.close()
        finally:
            self.inbox.close()
            self.temp.cleanup()

    def task(self, project, prompt):
        submission = parse_submission(json.dumps({
            "project_id": project, "title": "Parallel private execution",
            "prompt": prompt,
        }).encode())
        return self.inbox.accept(submission, OWNER).task_id

    def runner(self, kind, task_id):
        worker_id = kind + "-worker"
        gate = TaskPermissionGate(self.inbox, grants=(ActionGrant(
            task_id, worker_id, ProtectedAction.EXTERNAL_REQUEST),))
        command = (sys.executable, "-u", "-c", WAITING_WORKER)
        if kind == "gpt":
            result = GPTTaskExecutor(
                self.inbox, OWNER, worker_id=worker_id, model_id="gpt-offline",
                permission_gate=gate, timeout_seconds=20, worker_argv=command)
        else:
            result = ClaudeTaskExecutor(
                self.inbox, OWNER, authorized_owner_id=OWNER.owner_id,
                worker_id=worker_id, model_id="claude-offline",
                permission_gate=gate, timeout_seconds=20, worker_argv=command)
        self.runners.append(result)
        return result, gate

    def wait_both_started(self, futures):
        deadline = time.monotonic() + 8
        first = Path(self.root.resolve("gpt-slot")) / "started"
        second = Path(self.root.resolve("claude-slot")) / "started"
        while time.monotonic() < deadline:
            if first.exists() and second.exists():
                return first, second
            for future in futures:
                if future.done():
                    future.result()  # show real exception, not a vague timeout
            time.sleep(.01)
        self.fail("both real workers did not reach their execution barrier")

    def release(self):
        for workspace in ("gpt-slot", "claude-slot"):
            (Path(self.root.resolve(workspace)) / "release").touch()

    def test_gpt_and_claude_real_processes_overlap_and_commit_independent_results(self):
        gpt_id = self.task("project-a", "parallel A")
        claude_id = self.task("project-b", "parallel B")
        gpt, gpt_gate = self.runner("gpt", gpt_id)
        claude, claude_gate = self.runner("claude", claude_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(gpt.execute, gpt_id, "gpt-slot")
            second = pool.submit(claude.execute, claude_id, "claude-slot")
            try:
                marker_a, marker_b = self.wait_both_started((first, second))
                self.assertEqual(marker_a.read_text(), "gpt-offline")
                self.assertEqual(marker_b.read_text(), "claude-offline")
                self.assertFalse(first.done())
                self.assertFalse(second.done())
                self.assertIs(self.inbox.state(gpt_id, OWNER), TaskState.RUNNING)
                self.assertIs(self.inbox.state(claude_id, OWNER), TaskState.RUNNING)
                row_a = self.inbox._store.process_record(gpt_id)
                row_b = self.inbox._store.process_record(claude_id)
                self.assertIsNotNone(row_a)
                self.assertIsNotNone(row_b)
                self.assertNotEqual(row_a[1], row_b[1])  # distinct live PIDs
                self.assertEqual((row_a[7], row_b[7]), ("active", "active"))
                for name in ("gpt-slot", "claude-slot"):
                    with self.assertRaises(WorkspaceBusy):
                        self.root.acquire(name)
            finally:
                self.release()
            results = (first.result(timeout=10), second.result(timeout=10))

        self.assertEqual([(r.state, r.executed, r.text) for r in results], [
            (TaskState.SUCCEEDED, True, "gpt-offline:parallel A"),
            (TaskState.SUCCEEDED, True, "claude-offline:parallel B"),
        ])
        for task_id, runner, gate, provider in (
            (gpt_id, gpt, gpt_gate, "gpt"),
            (claude_id, claude, claude_gate, "claude"),
        ):
            self.assertEqual(runner.delivery.snapshot(task_id, OWNER).status,
                             "completed")
            self.assertEqual([(e.provider, e.outcome) for e in
                              runner.provider_history(task_id)],
                             [(provider, "completed")])
            self.assertEqual([e.allowed for e in gate.audit_history(task_id, OWNER)],
                             [True])
            self.assertEqual(self.inbox._store.process_record(task_id)[7], "stopped")
            self.assertFalse(runner.execute(task_id, "gpt-slot" if provider == "gpt"
                                                  else "claude-slot").executed)
            self.assertEqual(len(runner.provider_history(task_id)), 1)
        self.assertEqual(gpt.control.watchdog_errors(), ())
        self.assertEqual(claude.control.watchdog_errors(), ())

        # New SQLite owner can see both results; no re-dispatch on restart.
        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db_path),
                               workspace_root=self.root)
        for task_id in (gpt_id, claude_id):
            self.assertIs(self.inbox.state(task_id, OWNER), TaskState.SUCCEEDED)
            self.assertEqual(self.inbox._store.process_record(task_id)[7], "stopped")


if __name__ == "__main__":
    unittest.main()
