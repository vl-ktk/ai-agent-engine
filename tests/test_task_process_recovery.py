"""6.9: durable process identity, real SIGKILL supervisor and safe restart."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_control import TaskProcessController
from ai_agent_engine.task_delivery import TaskDeliveryManager
from ai_agent_engine.task_process_identity import (
    ProcessIdentityUnavailable, capture_private_leader,
    group_has_live_members, matches_leader, verified_leader_pidfd,
)
from ai_agent_engine.task_process_recovery import TaskProcessRecovery
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
OTHER = OwnerScope("other-owner", frozenset({"project-a"}))


class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-69-")
        self.base = Path(self.temp.name)
        self.root = self.base / "workspaces"
        self.root.mkdir(mode=0o700)
        self.workspace = self.root / "slot-a"
        self.workspace.mkdir(mode=0o700)
        self.db = self.base / "tasks.sqlite3"
        self.inbox = self.open_inbox()
        self.task_id = self.inbox.accept(parse_submission(
            b'{"project_id":"project-a","title":"Crash task","prompt":"offline"}'
        ), OWNER).task_id
        self.inbox.claim_task(self.task_id, "worker-a", "slot-a", OWNER)
        self.owned_processes = []

    def tearDown(self):
        for process in self.owned_processes:
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.inbox.close()
        self.temp.cleanup()

    def open_inbox(self):
        return TaskInbox(store=SQLiteTaskStore(self.db),
                         workspace_root=WorkspaceRoot(self.root))

    def reopen(self):
        self.inbox.close()
        self.inbox = self.open_inbox()

    def test_gated_success_registers_stable_identity_and_stops_ledger(self):
        controller = TaskProcessController(self.inbox)
        process = controller.launch_gated(
            self.task_id, OWNER, actor_id="worker-a",
            worker_argv=(sys.executable, "-c", "print('ready')"),
            cwd=str(self.workspace), environment={
                "PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path.cwd())},
            timeout_seconds=3)
        output, _ = process.communicate(timeout=3)
        self.assertEqual(output.strip(), b"ready")
        row = self.inbox._store.process_record(self.task_id)
        self.assertEqual((row[1], row[2], row[6], row[7]),
                         (process.pid, process.pid, "gated", "active"))
        self.assertIs(controller.finish(self.task_id, OWNER, success=True),
                      TaskState.SUCCEEDED)
        self.assertEqual(self.inbox._store.process_record(self.task_id)[7], "stopped")
        self.assertFalse(group_has_live_members(process.pid))
        controller.close()

    def test_closed_launch_gate_prevents_worker_execution(self):
        marker = self.base / "must-not-run"
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            process = subprocess.Popen(
                (sys.executable, "-m", "ai_agent_engine.task_launch_gate",
                 str(read_fd), sys.executable, "-c",
                 f"from pathlib import Path;Path({str(marker)!r}).touch()"),
                cwd=self.workspace, env={"PYTHONPATH": str(Path.cwd())},
                pass_fds=(read_fd,), start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.owned_processes.append(process)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 125)
        self.assertFalse(marker.exists())

    def test_registered_gate_lost_before_go_recovers_without_worker_execution(self):
        marker = self.base / "never-executed"
        controller = TaskProcessController(self.inbox)
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            process = subprocess.Popen(
                (sys.executable, "-m", "ai_agent_engine.task_launch_gate",
                 str(read_fd), sys.executable, "-c",
                 f"from pathlib import Path;Path({str(marker)!r}).touch()"),
                cwd=self.workspace, env={"PYTHONPATH": str(Path.cwd())},
                pass_fds=(read_fd,), start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.owned_processes.append(process)
            controller.attach(self.task_id, process, OWNER, actor_id="worker-a",
                              _launch_kind="gated")
            self.assertEqual(self.inbox._store.process_record(self.task_id)[7],
                             "active")
        finally:
            os.close(read_fd)
            os.close(write_fd)  # supervisor died before sending G
        process.communicate(timeout=3)
        self.assertFalse(marker.exists())
        self.reopen()
        self.assertEqual(TaskProcessRecovery(self.inbox, OWNER).recover()[0].status,
                         "stopped")
        self.assertIs(self.inbox.state(self.task_id, OWNER), TaskState.CANCELLED)

    def test_recovery_kills_entire_private_group_with_grandchild(self):
        marker = self.base / "grandchild.pid"
        code = (
            "import subprocess,sys,time,pathlib;"
            "p=subprocess.Popen((sys.executable,'-c','import time;time.sleep(90)'));"
            f"pathlib.Path({str(marker)!r}).write_text(str(p.pid));"
            "time.sleep(90)"
        )
        process = subprocess.Popen(
            (sys.executable, "-c", code), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.owned_processes.append(process)
        controller = TaskProcessController(self.inbox)
        controller.attach(self.task_id, process, OWNER, actor_id="worker-a")
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        grandchild = int(marker.read_text())
        self.assertTrue(group_has_live_members(process.pid))
        self.reopen()  # emulate crash: discard supervisor object, not child
        self.assertEqual(TaskProcessRecovery(self.inbox, OWNER).recover()[0].status,
                         "stopped")
        self.assertFalse(group_has_live_members(process.pid))
        if Path(f"/proc/{grandchild}/stat").exists():
            state = Path(f"/proc/{grandchild}/stat").read_text().rsplit(")", 1)[1].split()[0]
            self.assertIn(state, ("Z", "X", "x"))

    def test_sigkill_supervisor_recovery_stops_orphan_and_frees_workspace(self):
        self.inbox.close()  # Only the disposable supervisor owns the DB lock.
        marker = self.base / "ready.pid"
        worker = "import time;time.sleep(90)"
        supervisor_code = (
            "import os,sys,time;from pathlib import Path;"
            "from ai_agent_engine.task_api import OwnerScope,TaskInbox;"
            "from ai_agent_engine.task_assignment import WorkspaceRoot;"
            "from ai_agent_engine.task_control import TaskProcessController;"
            "from ai_agent_engine.task_delivery import TaskDeliveryManager;"
            "from ai_agent_engine.task_storage import SQLiteTaskStore;"
            "base=Path(sys.argv[1]);task=sys.argv[2];marker=Path(sys.argv[3]);"
            "scope=OwnerScope('owner-a',frozenset({'project-a'}));"
            "inbox=TaskInbox(store=SQLiteTaskStore(base/'tasks.sqlite3'),"
            "workspace_root=WorkspaceRoot(base/'workspaces'));"
            "TaskDeliveryManager(inbox).reserve(task,scope,actor_id='worker-a',max_attempts=1);"
            "controller=TaskProcessController(inbox);"
            "p=controller.launch_gated(task,scope,actor_id='worker-a',"
            "worker_argv=(sys.executable,'-c'," + repr(worker) + "),"
            "cwd=str(base/'workspaces'/'slot-a'),"
            "environment={'PATH':'/usr/bin:/bin','PYTHONPATH':str(Path.cwd())},"
            "timeout_seconds=120);"
            "marker.write_text(str(p.pid));time.sleep(120)"
        )
        supervisor = subprocess.Popen(
            (sys.executable, "-u", "-c", supervisor_code,
             str(self.base), self.task_id, str(marker)),
            cwd=Path.cwd(), env={"PATH": "/usr/bin:/bin",
                                 "PYTHONPATH": str(Path.cwd())},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True)
        pid = None
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                if supervisor.poll() is not None:
                    raise AssertionError("trusted supervisor exited before gate release")
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            pid = int(marker.read_text())
            self.assertTrue(group_has_live_members(pid))
            os.kill(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=3)
            self.assertTrue(group_has_live_members(pid))  # actual orphan

            self.inbox = self.open_inbox()
            delivery = TaskDeliveryManager(self.inbox)
            self.assertTrue(delivery.recovery_inventory(OWNER)[0].review_required)
            results = TaskProcessRecovery(self.inbox, OWNER).recover()
            self.assertEqual([(r.task_id, r.status) for r in results],
                             [(self.task_id, "stopped")])
            self.assertFalse(group_has_live_members(pid))
            self.assertIs(self.inbox.state(self.task_id, OWNER), TaskState.CANCELLED)
            self.assertEqual(self.inbox._store.process_record(self.task_id)[7], "stopped")
            self.assertEqual(delivery.recovery_inventory(OWNER), ())
            self.assertEqual(delivery.snapshot(self.task_id, OWNER).status, "active")
            self.assertEqual(TaskProcessRecovery(self.inbox, OWNER).recover(), ())
            another = self.inbox.accept(parse_submission(
                b'{"project_id":"project-a","title":"Next","prompt":"safe"}'
            ), OWNER).task_id
            self.inbox.claim_task(another, "worker-a", "slot-a", OWNER)
        finally:
            if supervisor.poll() is None:
                os.kill(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=3)
            supervisor.stderr.close()
            if pid is not None and group_has_live_members(pid):
                # Exact test-owned group; never leave a live orphan on failure.
                os.killpg(pid, signal.SIGKILL)
            if self.inbox._store is None:
                self.inbox = self.open_inbox()

    def test_stale_pid_start_time_is_held_not_signalled(self):
        process = subprocess.Popen(
            (sys.executable, "-c", "import time;time.sleep(60)"),
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.owned_processes.append(process)
        identity = capture_private_leader(process.pid)
        self.inbox._store.register_process(
            self.task_id, "worker-a", pid=process.pid, pgid=identity.pgid,
            sid=identity.sid, start_ticks=identity.start_ticks + 1,
            boot_id=identity.boot_id, launch_kind="direct")
        self.inbox.transition_state(self.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")
        result = TaskProcessRecovery(self.inbox, OWNER).recover()
        self.assertEqual(result[0].status, "held_identity")
        self.assertIsNone(process.poll())
        self.assertIs(self.inbox.state(self.task_id, OWNER), TaskState.RUNNING)
        self.assertEqual(self.inbox._store.process_record(self.task_id)[7], "active")

    def test_running_without_process_ledger_remains_on_hold(self):
        self.inbox.transition_state(self.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")
        result = TaskProcessRecovery(self.inbox, OWNER).recover()
        self.assertEqual(result[0].status, "held_untracked")
        self.assertIs(self.inbox.state(self.task_id, OWNER), TaskState.RUNNING)

    def test_other_owner_cannot_recover_task(self):
        self.inbox.transition_state(self.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")
        self.assertEqual(TaskProcessRecovery(self.inbox, OTHER).recover(), ())
        self.assertIs(self.inbox.state(self.task_id, OWNER), TaskState.RUNNING)

    def test_assigned_without_process_is_not_considered_running_orphan(self):
        self.assertEqual(TaskProcessRecovery(self.inbox, OWNER).recover(), ())
        self.assertIs(self.inbox.state(self.task_id, OWNER), TaskState.ASSIGNED)

    def test_pidfd_pins_matching_live_process_identity(self):
        process = subprocess.Popen(
            (sys.executable, "-c", "import time;time.sleep(60)"),
            start_new_session=True, stdout=subprocess.DEVNULL)
        self.owned_processes.append(process)
        identity = capture_private_leader(process.pid)
        self.assertTrue(matches_leader(identity))
        fd = verified_leader_pidfd(identity)
        os.close(fd)
        with self.assertRaises(ProcessIdentityUnavailable):
            verified_leader_pidfd(type(identity)(
                identity.pid, identity.pgid, identity.sid,
                identity.start_ticks + 1, identity.boot_id))

    def test_v7_private_database_migrates_to_v8_process_ledger(self):
        self.inbox.close()
        import sqlite3
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE task_process_runs")
            db.execute("PRAGMA user_version=7")
        self.inbox = self.open_inbox()
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (15,))
        self.assertIsNone(self.inbox._store.process_record(self.task_id))


if __name__ == "__main__":
    unittest.main()
