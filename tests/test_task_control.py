"""Stage 5.11: terminate real disposable local processes before terminal status."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import OwnerScope, TaskInbox, TaskNotFound, parse_submission
from ai_agent_engine.task_assignment import WorkspaceBusy, WorkspaceRoot
from ai_agent_engine.task_control import TaskControlError, TaskProcessController
from ai_agent_engine.task_state import TaskState, TransitionRejected
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("owner-b", frozenset({"project-a"}))
SLEEP = "import time; time.sleep(60)"


def submission():
    return parse_submission(json.dumps({
        "project_id": "project-a", "title": "Inert test job",
        "prompt": "PRIVATE PROMPT",
    }).encode())


class TaskProcessControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-511-")
        base = Path(self.temp.name)
        workspace = base / "workspaces"
        workspace.mkdir(mode=0o700)
        for slot in ("slot-a", "slot-b"):
            (workspace / slot).mkdir(mode=0o700)
        self.root = WorkspaceRoot(workspace)
        self.db = base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.control = TaskProcessController(self.inbox, grace_seconds=0.06,
                                             check_interval=0.01)
        self.processes = []

    def tearDown(self):
        try:
            self.control.close()
        finally:
            for process in self.processes:
                try:
                    if process.poll() is None:
                        if os.getpgid(process.pid) == process.pid:
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            self.inbox.close()
            self.temp.cleanup()

    def create(self):
        return self.inbox.accept(submission(), OWNER).task_id

    def claim(self, task_id, slot="slot-a"):
        return self.inbox.claim_task(task_id, "worker-a", slot, OWNER)

    def spawn(self, code=SLEEP):
        process = subprocess.Popen(
            [sys.executable, "-c", code], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def attach(self, task_id, process, timeout=None):
        self.control.attach(task_id, process, OWNER, actor_id="worker-a",
                            timeout_seconds=timeout)

    def wait_state(self, task_id, expected, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.inbox.state(task_id, OWNER) is expected:
                return
            time.sleep(0.015)
        self.fail(f"never reached {expected.value}: {self.inbox.state(task_id, OWNER)}")

    def restart(self):
        self.control.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.control = TaskProcessController(self.inbox)

    def test_queued_cancel_is_durable_and_has_single_audit_event(self):
        task_id = self.create()
        self.assertIs(self.control.cancel(task_id, OWNER), TaskState.CANCELLED)
        self.assertIsNone(self.inbox.select_next("project-a", OWNER))
        self.assertEqual([(e.to_state, e.accepted) for e in
                          self.inbox.transition_history(task_id, OWNER)],
                         [(TaskState.CANCELLED, True)])
        self.restart()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)

    def test_assigned_cancel_releases_workspace(self):
        first, second = self.create(), self.create()
        self.claim(first)
        self.assertIs(self.control.cancel(first, OWNER), TaskState.CANCELLED)
        self.claim(second)
        self.assertIs(self.inbox.state(second, OWNER), TaskState.ASSIGNED)

    def test_unmanaged_running_refuses_false_cancellation_and_keeps_lock(self):
        first, second = self.create(), self.create()
        self.claim(first)
        self.inbox.transition_state(first, TaskState.RUNNING, OWNER,
                                    actor_id="worker-a")
        with self.assertRaises(TaskControlError):
            self.control.cancel(first, OWNER)
        self.assertIs(self.inbox.state(first, OWNER), TaskState.RUNNING)
        with self.assertRaises(WorkspaceBusy):
            self.claim(second)

    def test_cancel_kills_real_child_before_status_and_frees_workspace(self):
        first, second = self.create(), self.create()
        self.claim(first)
        process = self.spawn()
        self.attach(first, process)
        self.assertIs(self.inbox.state(first, OWNER), TaskState.RUNNING)
        self.assertIs(self.control.cancel(first, OWNER), TaskState.CANCELLED)
        self.assertIsNotNone(process.poll())
        self.claim(second)
        self.assertEqual([e.to_state for e in self.inbox.transition_history(first, OWNER)],
                         [TaskState.ASSIGNED, TaskState.RUNNING, TaskState.CANCELLED])
        self.restart()
        self.assertIs(self.inbox.state(first, OWNER), TaskState.CANCELLED)
        self.assertIs(self.inbox.state(second, OWNER), TaskState.ASSIGNED)

    def test_timeout_kills_process_before_status_and_survives_restart(self):
        task_id = self.create()
        self.claim(task_id)
        process = self.spawn()
        self.attach(task_id, process, timeout=0.08)
        self.wait_state(task_id, TaskState.TIMED_OUT)
        self.assertIsNotNone(process.poll())
        self.assertEqual(self.control.watchdog_errors, ())
        self.restart()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.TIMED_OUT)
        next_id = self.create()
        self.claim(next_id)

    def test_sigterm_ignoring_child_is_killed_on_cancel(self):
        task_id = self.create()
        self.claim(task_id)
        marker = Path(self.temp.name) / "handler-ready"
        process = self.spawn("import pathlib,signal,time; "
                             "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                             f"pathlib.Path({str(marker)!r}).touch(); time.sleep(60)")
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        self.attach(task_id, process)
        self.control.cancel(task_id, OWNER)
        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_exited_parent_with_live_grandchild_cannot_finish_or_escape_cancel(self):
        task_id = self.create()
        self.claim(task_id)
        marker = Path(self.temp.name) / "grandchild-pid"
        script = ("import subprocess,sys,pathlib,time; "
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                  f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); "
                  "time.sleep(0.15)")
        parent = self.spawn(script)
        self.attach(task_id, parent)
        parent.wait(timeout=2)
        grandchild = int(marker.read_text())
        self.assertTrue(self.control._group_running(parent.pid))
        with self.assertRaises(TaskControlError):
            self.control.finish(task_id, OWNER, success=True)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        self.control.cancel(task_id, OWNER)
        self.assertFalse(self.control._group_running(parent.pid))
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)
        # The descendant may briefly remain as an unreaped zombie, but is not live.
        if Path(f"/proc/{grandchild}/stat").exists():
            state = Path(f"/proc/{grandchild}/stat").read_text().rsplit(")", 1)[1].split()[0]
            self.assertIn(state, ("Z", "X", "x"))

    def test_success_and_failure_finish_follow_observed_exit_code(self):
        for code, expected in ((0, TaskState.SUCCEEDED), (7, TaskState.FAILED)):
            with self.subTest(code=code):
                task_id = self.create()
                self.claim(task_id)
                process = self.spawn(f"import time; time.sleep(0.07); raise SystemExit({code})")
                self.attach(task_id, process)
                process.wait(timeout=2)
                self.assertIs(self.control.finish(task_id, OWNER, success=code == 0),
                              expected)
                self.assertIs(self.inbox.state(task_id, OWNER), expected)

    def test_gated_pre_release_callback_does_not_execute_on_denial(self):
        task_id = self.create()
        assignment = self.claim(task_id)
        marker = Path(assignment.workspace_dir) / "should-not-exist"
        worker = (sys.executable, "-c",
                  f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')")

        def denied():
            self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
            self.assertFalse(marker.exists())
            raise TaskControlError("permission_preflight_denied")

        with self.assertRaisesRegex(TaskControlError, "permission_preflight_denied"):
            self.control.launch_gated(
                task_id, OWNER, actor_id="worker-a", worker_argv=worker,
                cwd=assignment.workspace_dir, environment={"PATH": "/usr/bin:/bin"},
                quiet=True, before_release=denied)
        self.assertFalse(marker.exists())
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)

    def test_finish_refuses_live_child_and_mismatched_result(self):
        task_id = self.create()
        self.claim(task_id)
        process = self.spawn()
        self.attach(task_id, process)
        with self.assertRaises(TaskControlError):
            self.control.finish(task_id, OWNER, success=True)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        self.control.cancel(task_id, OWNER)
        with self.assertRaises(TransitionRejected):
            self.control.finish(task_id, OWNER, success=False)

    def test_mismatched_result_does_not_commit_success(self):
        task_id = self.create()
        self.claim(task_id)
        process = self.spawn("import time; time.sleep(0.07); raise SystemExit(9)")
        self.attach(task_id, process)
        process.wait(timeout=2)
        with self.assertRaises(TaskControlError):
            self.control.finish(task_id, OWNER, success=True)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        self.assertIs(self.control.finish(task_id, OWNER, success=False),
                      TaskState.FAILED)

    def test_wrong_actor_and_scope_never_take_process_ownership(self):
        task_id = self.create()
        self.claim(task_id)
        process = self.spawn()
        with self.assertRaises(TaskControlError):
            self.control.attach(task_id, process, OWNER, actor_id="wrong-worker")
        with self.assertRaises(TaskNotFound):
            self.control.attach(task_id, process, FOREIGN, actor_id="worker-a")
        with self.assertRaises(TaskNotFound):
            self.control.cancel(task_id, FOREIGN)
        self.assertIsNone(process.poll())
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)

    def test_unisolated_process_is_rejected_without_signalling(self):
        task_id = self.create()
        self.claim(task_id)
        process = subprocess.Popen([sys.executable, "-c", SLEEP],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.processes.append(process)
        with self.assertRaises(TaskControlError):
            self.attach(task_id, process)
        self.assertIsNone(process.poll())
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)

    def test_cancel_and_timeout_race_has_exactly_one_terminal_event(self):
        task_id = self.create()
        self.claim(task_id)
        process = self.spawn()
        self.attach(task_id, process, timeout=0.045)
        time.sleep(0.025)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.control.cancel, task_id, OWNER)
            try:
                future.result(timeout=3)
            except TransitionRejected:
                pass
        state = self.inbox.state(task_id, OWNER)
        self.assertIn(state, (TaskState.CANCELLED, TaskState.TIMED_OUT))
        self.assertIsNotNone(process.poll())
        events = self.inbox.transition_history(task_id, OWNER)
        self.assertEqual(sum(e.accepted and e.to_state in
                             (TaskState.CANCELLED, TaskState.TIMED_OUT)
                             for e in events), 1)

    def test_two_independent_processes_cancel_one_only(self):
        first, second = self.create(), self.create()
        self.claim(first)
        self.claim(second, "slot-b")
        p1, p2 = self.spawn(), self.spawn()
        self.attach(first, p1)
        self.attach(second, p2)
        self.control.cancel(first, OWNER)
        self.assertIsNotNone(p1.poll())
        self.assertIsNone(p2.poll())
        self.assertIs(self.inbox.state(second, OWNER), TaskState.RUNNING)
        self.control.cancel(second, OWNER)

    def test_same_process_cannot_be_claimed_by_two_tasks(self):
        first, second = self.create(), self.create()
        self.claim(first)
        self.claim(second, "slot-b")
        process = self.spawn()
        self.attach(first, process)
        with self.assertRaises(TaskControlError):
            self.attach(second, process)
        self.assertIs(self.inbox.state(second, OWNER), TaskState.ASSIGNED)
        self.control.cancel(first, OWNER)

    def test_finish_cannot_mark_success_after_deadline_expired(self):
        self.control.close()
        self.control = TaskProcessController(self.inbox, check_interval=0.3)
        task_id = self.create()
        self.claim(task_id)
        process = self.spawn("import time; time.sleep(0.06)")
        self.attach(task_id, process, timeout=0.02)
        process.wait(timeout=2)
        with self.assertRaises(TaskControlError):
            self.control.finish(task_id, OWNER, success=True)
        self.wait_state(task_id, TaskState.TIMED_OUT)

    def test_close_cleans_all_attached_tasks_and_watchdog(self):
        first, second = self.create(), self.create()
        self.claim(first)
        self.claim(second, "slot-b")
        p1, p2 = self.spawn(), self.spawn()
        self.attach(first, p1, timeout=60)
        self.attach(second, p2)
        self.control.close()
        self.control.close()
        self.assertIsNotNone(p1.poll())
        self.assertIsNotNone(p2.poll())
        self.assertIs(self.inbox.state(first, OWNER), TaskState.CANCELLED)
        self.assertIs(self.inbox.state(second, OWNER), TaskState.CANCELLED)

    def test_sqlite_commit_failure_keeps_state_and_lease_until_retry(self):
        first, second = self.create(), self.create()
        self.claim(first)
        process = self.spawn()
        self.attach(first, process)
        with patch.object(self.inbox._store, "write_transition",
                          side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                self.control.cancel(first, OWNER)
        self.assertIsNotNone(process.poll())
        self.assertIs(self.inbox.state(first, OWNER), TaskState.RUNNING)
        with self.assertRaises(WorkspaceBusy):
            self.claim(second)
        self.assertIs(self.control.cancel(first, OWNER), TaskState.CANCELLED)
        self.claim(second)

    def test_timeout_storage_failure_is_not_acknowledged_and_retries(self):
        first, second = self.create(), self.create()
        self.claim(first)
        process = self.spawn()
        self.attach(first, process, timeout=0.04)
        with patch.object(self.inbox._store, "write_transition",
                          side_effect=StorageUnavailable("offline")):
            deadline = time.monotonic() + 2
            while not self.control.watchdog_errors and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.control.watchdog_errors)
            self.assertIsNotNone(process.poll())
            self.assertIs(self.inbox.state(first, OWNER), TaskState.RUNNING)
            with self.assertRaises(WorkspaceBusy):
                self.claim(second)
        self.wait_state(first, TaskState.TIMED_OUT)
        self.claim(second)

    def test_invalid_intervals_or_process_rejected(self):
        with self.assertRaises(ValueError):
            TaskProcessController(self.inbox, grace_seconds=0)
        task_id = self.create()
        self.claim(task_id)
        with self.assertRaises(TaskControlError):
            self.attach(task_id, object())
        process = self.spawn()
        with self.assertRaises(ValueError):
            self.attach(task_id, process, timeout=0)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)


if __name__ == "__main__":
    unittest.main()
