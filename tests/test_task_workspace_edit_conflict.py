"""Stage 6.11: conflicting GPT/Claude edits must never share a live workspace.

Real Linux subprocesses, but deterministic offline workers (no LLM/API calls).
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


# The marker is a simulated edit. A losing worker must never create its marker.
# The first worker holds its real process and workspace lock until released.
WORKER_CODE = (
    "import json,pathlib,sys,time;"
    "request=json.loads(sys.stdin.buffer.read());"
    "pathlib.Path('edit-'+request['model_id']).write_text('executed');"
    "deadline=time.monotonic()+12;"
    "\nwhile not pathlib.Path('release').exists():"
    "\n if time.monotonic()>deadline: raise SystemExit(12)"
    "\n time.sleep(.01)"
    "\nprint(json.dumps({'ok':True,'text':request['model_id']+':done'}),flush=True)"
)


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                     "requires Linux private process groups and pidfd")
class TaskWorkspaceEditConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-611-")
        self.base = Path(self.temp.name)
        root = self.base / "workspaces"
        root.mkdir(mode=0o700)
        (root / "shared").mkdir(mode=0o700)
        (root / "unrelated").mkdir(mode=0o700)
        self.workspace = root / "shared"
        self.root = WorkspaceRoot(root)
        self.db_path = self.base / "private-tasks.sqlite3"
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

    def task(self, project):
        record = parse_submission(json.dumps({
            "project_id": project,
            "title": "Exclusive workspace edit",
            "prompt": "SYNTHETIC LOCAL OPERATION ONLY",
        }).encode())
        return self.inbox.accept(record, OWNER).task_id

    def runner(self, kind, task_id):
        worker_id = kind + "-worker"
        gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(task_id, worker_id, ProtectedAction.EXTERNAL_REQUEST),))
        args = (sys.executable, "-u", "-c", WORKER_CODE)
        if kind == "gpt":
            runner = GPTTaskExecutor(
                self.inbox, OWNER, worker_id=worker_id,
                model_id="gpt-611", permission_gate=gate,
                timeout_seconds=16, worker_argv=args)
        else:
            runner = ClaudeTaskExecutor(
                self.inbox, OWNER, authorized_owner_id=OWNER.owner_id,
                worker_id=worker_id, model_id="claude-611",
                permission_gate=gate, timeout_seconds=16, worker_argv=args)
        self.runners.append(runner)
        return runner, gate

    def await_edit_marker(self, kind, future):
        marker = self.workspace / ("edit-" + kind + "-611")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if marker.exists():
                return
            if future.done():
                future.result()  # surface a real error instead of a timeout
            time.sleep(.01)
        self.fail("first real worker did not reach edit barrier")

    def prove_conflict(self, first_kind, second_kind):
        first_id = self.task("project-a")
        second_id = self.task("project-b")
        first, first_gate = self.runner(first_kind, first_id)
        second, second_gate = self.runner(second_kind, second_id)
        marker = self.workspace / ("edit-" + second_kind + "-611")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first.execute, first_id, "shared")
            try:
                self.await_edit_marker(first_kind, future)
                self.assertIs(self.inbox.state(first_id, OWNER), TaskState.RUNNING)
                self.assertIsNotNone(self.inbox._store.process_record(first_id))
                self.assertFalse(future.done())
                with self.assertRaisesRegex(
                    WorkspaceBusy, "already in use by another active task"
                ):
                    second.execute(second_id, "shared")

                # The loser neither edits nor sends a request; its task remains queued.
                self.assertFalse(marker.exists())
                self.assertIs(self.inbox.state(second_id, OWNER), TaskState.QUEUED)
                self.assertIsNone(self.inbox.get_assignment(second_id, OWNER))
                self.assertIsNone(second.delivery.snapshot(second_id, OWNER))
                self.assertIsNone(self.inbox._store.process_record(second_id))
                self.assertEqual(second.provider_history(second_id), ())
                self.assertEqual(second_gate.audit_history(second_id, OWNER), ())
                self.assertEqual(
                    self.inbox.transition_history(second_id, OWNER)[-1].reason_code,
                    "workspace_busy")
                self.assertEqual(len(first_gate.audit_history(first_id, OWNER)), 1)
            finally:
                (self.workspace / "release").touch()
            first_result = future.result(timeout=10)

        self.assertIs(first_result.state, TaskState.SUCCEEDED)
        self.assertEqual(first_result.text, first_kind + "-611:done")
        self.assertEqual(self.inbox._store.process_record(first_id)[7], "stopped")
        # A legitimate retry only starts after the first worker releases its lease.
        second_result = second.execute(second_id, "shared")
        self.assertEqual((second_result.state, second_result.executed),
                         (TaskState.SUCCEEDED, True))
        self.assertTrue(marker.exists())
        self.assertEqual(second_result.text, second_kind + "-611:done")
        self.assertEqual([e.outcome for e in second.provider_history(second_id)],
                         ["completed"])
        self.assertEqual([e.allowed for e in second_gate.audit_history(second_id, OWNER)],
                         [True])
        self.assertEqual(second.delivery.snapshot(second_id, OWNER).attempt, 1)
        self.assertFalse(second.execute(second_id, "shared").executed)

        # The first and second tasks have independent durable results and histories.
        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db_path),
                               workspace_root=self.root)
        self.assertIs(self.inbox.state(first_id, OWNER), TaskState.SUCCEEDED)
        self.assertIs(self.inbox.state(second_id, OWNER), TaskState.SUCCEEDED)
        self.assertEqual(
            [e.reason_code for e in self.inbox.transition_history(second_id, OWNER)],
            ["workspace_busy", "claim", "transition", "transition"])

    def test_gpt_holds_shared_workspace_claude_fails_closed_then_retries(self):
        self.prove_conflict("gpt", "claude")

    def test_claude_holds_shared_workspace_gpt_fails_closed_then_retries(self):
        self.prove_conflict("claude", "gpt")


if __name__ == "__main__":
    unittest.main()
