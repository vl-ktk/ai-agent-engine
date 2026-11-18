"""5.14: offline whole-stage concurrency, crash/restart and reconciliation.

All processes, workspaces, database mutations and permission callbacks use
disposable local fixtures. No LLM, external API, server or paid service.
"""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission, task_handler
from ai_agent_engine.task_assignment import WorkspaceBusy, WorkspaceRoot
from ai_agent_engine.task_control import TaskProcessController
from ai_agent_engine.task_delivery import TaskDeliveryManager
from ai_agent_engine.task_permissions import (
    ActionGrant, PermissionDenied, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("owner-b", frozenset({"project-a"}))
SLEEP = "import time; time.sleep(60)"


class StageFiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-514-")
        self.base = Path(self.temp.name)
        dirs = self.base / "workspaces"
        dirs.mkdir(mode=0o700)
        for index in range(12):
            (dirs / f"slot-{index:02d}").mkdir(mode=0o700)
        self.root = WorkspaceRoot(dirs)
        self.db = self.base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.delivery = TaskDeliveryManager(self.inbox, retry_delay_seconds=0)
        self.control = TaskProcessController(self.inbox, grace_seconds=0.05,
                                             check_interval=0.01)
        self.processes = []
        self.orphans = []

    def tearDown(self):
        try:
            self.control.close()
        finally:
            for pid in self.orphans:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for process in self.processes:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=3)
            self.inbox.close()
            self.temp.cleanup()

    def create(self, *, key=None, title="Disposable offline task", scope=OWNER):
        submission = parse_submission(json.dumps({
            "project_id": "project-a", "title": title,
            "prompt": "Never execute task prompt as a command",
        }).encode())
        return self.inbox.accept(submission, scope, idempotency_key=key).task_id

    def claim(self, task_id, index=0, *, scope=OWNER):
        return self.inbox.claim_task(task_id, "worker-a", f"slot-{index:02d}", scope)

    def reserve(self, task_id, *, scope=OWNER, max_attempts=2):
        return self.delivery.reserve(task_id, scope, actor_id="worker-a",
                                     max_attempts=max_attempts)

    def spawn(self, code=SLEEP):
        process = subprocess.Popen([sys.executable, "-c", code],
                                   start_new_session=True,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def restart(self):
        self.control.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.delivery = TaskDeliveryManager(self.inbox, retry_delay_seconds=0)
        self.control = TaskProcessController(self.inbox, grace_seconds=0.05,
                                             check_interval=0.01)

    def wait_for_state(self, task_id, state):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            if self.inbox.state(task_id, OWNER) is state:
                return
            time.sleep(0.01)
        self.fail(f"task did not enter {state.value}")

    def test_eight_independent_real_processes_concurrently_finish_and_recover(self):
        tasks = [self.create(key=f"work-{i}") for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda pair: self.claim(*pair),
                                   ((task_id, i) for i, task_id in enumerate(tasks))))
            tickets = list(pool.map(self.reserve, tasks))
        self.assertEqual(len({claim.workspace_dir for claim in claims}), 8)
        self.assertTrue(all(ticket.should_execute for ticket in tickets))
        processes = [self.spawn("import time; time.sleep(0.3)") for _ in tasks]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda args: self.control.attach(
                args[0], args[1], OWNER, actor_id="worker-a"),
                zip(tasks, processes)))
        for process in processes:
            self.assertEqual(process.wait(timeout=3), 0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda task_id: self.control.finish(
                task_id, OWNER, success=True), tasks))
            acknowledgements = list(pool.map(lambda arg: self.delivery.report(
                arg[0], OWNER, actor_id="worker-a", token=arg[1].token,
                success=True), zip(tasks, tickets)))
        self.assertEqual(states, [TaskState.SUCCEEDED] * 8)
        self.assertTrue(all(row.status == "completed" for row in acknowledgements))
        self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
        self.restart()
        self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
        self.assertTrue(all(self.inbox.state(t, OWNER) is TaskState.SUCCEEDED
                            for t in tasks))
        self.assertTrue(all(not TaskProcessController._group_running(p.pid)
                            for p in processes))

    def test_sixteen_concurrent_claims_only_one_workspace_owner(self):
        tasks = [self.create(title=f"claim-{i}") for i in range(16)]

        def attempt(task_id):
            try:
                self.claim(task_id)
                return task_id
            except WorkspaceBusy:
                return None

        with ThreadPoolExecutor(max_workers=16) as pool:
            winners = list(pool.map(attempt, tasks))
        self.assertEqual(sum(w is not None for w in winners), 1)
        winner = next(w for w in winners if w is not None)
        ticket = self.reserve(winner)
        process = self.spawn("pass")
        self.control.attach(winner, process, OWNER, actor_id="worker-a")
        process.wait(timeout=3)
        self.assertIs(self.control.finish(winner, OWNER, success=True),
                      TaskState.SUCCEEDED)
        self.delivery.report(winner, OWNER, actor_id="worker-a",
                             token=ticket.token, success=True)
        loser = next(t for t in tasks if t != winner)
        self.claim(loser)
        self.assertEqual(self.inbox.state(loser, OWNER), TaskState.ASSIGNED)
        self.assertEqual(len(self.delivery.recovery_inventory(OWNER)), 15)
        self.restart()  # close() of reopened controller must not mark unstarted claims
        record = next(r for r in self.delivery.recovery_inventory(OWNER)
                      if r.task_id == loser)
        self.assertTrue(record.review_required)

    def test_local_http_intake_to_durable_worker_and_audit_end_to_end(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), task_handler(self.inbox, OWNER))
        from threading import Thread
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"project_id": "project-a", "title": "HTTP job",
                               "prompt": "Inert"}).encode()

            def request(path, payload, headers=None):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request("POST", path, body=payload,
                                       headers={"Content-Type": "application/json",
                                                **(headers or {})})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            first_status, first = request("/v1/tasks", body,
                                          {"Idempotency-Key": "whole-stage"})
            second_status, second = request("/v1/tasks", body,
                                            {"Idempotency-Key": "whole-stage"})
            self.assertEqual((first_status, second_status), (202, 202))
            self.assertEqual(first["task_id"], second["task_id"])
            task_id = first["task_id"]
            claim_status, _ = request(f"/v1/tasks/{task_id}/claim",
                                       json.dumps({"worker_id": "worker-a",
                                                   "workspace_name": "slot-00"}).encode())
            self.assertEqual(claim_status, 201)
            ticket = self.reserve(task_id)
            gate = TaskPermissionGate(self.inbox, grants=(ActionGrant(
                task_id, "worker-a", ProtectedAction.EXECUTE_TOOL),))
            self.control = TaskProcessController(self.inbox, permission_gate=gate)
            process = self.spawn()
            self.control.attach(task_id, process, OWNER, actor_id="worker-a")
            with self.assertRaises(PermissionDenied):
                self.control.execute_protected(
                    task_id, OWNER, actor_id="worker-a",
                    action=ProtectedAction.EXTERNAL_REQUEST,
                    operation=lambda: self.fail("must remain blocked"))
            self.assertEqual(self.control.execute_protected(
                task_id, OWNER, actor_id="worker-a",
                action=ProtectedAction.EXECUTE_TOOL,
                operation=lambda: "trusted_fixture"), "trusted_fixture")
            self.assertIs(self.control.cancel(task_id, OWNER), TaskState.CANCELLED)
            self.assertEqual(len(gate.audit_history(task_id, OWNER)), 2)
            self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
            self.assertIsNotNone(ticket.token)
            self.restart()
            self.assertIs(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)
            self.assertEqual(len(TaskPermissionGate(self.inbox).audit_history(
                task_id, OWNER)), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_two_actual_attempts_one_workspace_with_durable_token(self):
        task_id = self.create(key="retry-once")
        self.claim(task_id)
        first = self.reserve(task_id)
        failure = self.spawn("import sys; sys.exit(3)")
        self.control.attach(task_id, failure, OWNER, actor_id="worker-a")
        self.assertEqual(failure.wait(timeout=3), 3)
        self.assertEqual(self.control.complete_failed_attempt(task_id, OWNER), 3)
        self.assertEqual(self.delivery.report(task_id, OWNER, actor_id="worker-a",
                                              token=first.token,
                                              success=False).status, "retryable")
        second = self.reserve(task_id)
        self.assertTrue(second.should_execute)
        self.assertEqual(second.attempt, 2)
        success = self.spawn("pass")
        self.control.attach_retry(task_id, success, OWNER, actor_id="worker-a",
                                  delivery_token=second.token)
        self.assertEqual(success.wait(timeout=3), 0)
        self.assertIs(self.control.finish(task_id, OWNER, success=True),
                      TaskState.SUCCEEDED)
        self.delivery.report(task_id, OWNER, actor_id="worker-a",
                             token=second.token, success=True)
        self.restart()
        self.assertEqual(self.delivery.snapshot(task_id, OWNER).status, "completed")
        self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
        self.assertFalse(self.reserve(task_id).should_execute)

    def test_cancel_kills_process_group_and_releases_workspace(self):
        task_id = self.create()
        self.claim(task_id)
        self.reserve(task_id)
        process = self.spawn("import subprocess,sys,time; "
                             "subprocess.Popen([sys.executable,'-c',"
                             "'import time;time.sleep(60)']); time.sleep(60)")
        self.control.attach(task_id, process, OWNER, actor_id="worker-a")
        time.sleep(0.05)
        self.assertIs(self.control.cancel(task_id, OWNER), TaskState.CANCELLED)
        self.assertFalse(TaskProcessController._group_running(process.pid))
        self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
        next_id = self.create()
        self.claim(next_id)
        self.restart()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)
        self.assertEqual(self.inbox.state(next_id, OWNER), TaskState.ASSIGNED)

    def test_timeout_while_another_real_process_succeeds(self):
        slow, fast = self.create(title="slow"), self.create(title="fast")
        self.claim(slow, 0)
        self.claim(fast, 1)
        self.reserve(slow)
        fast_ticket = self.reserve(fast)
        slow_process = self.spawn(SLEEP)
        fast_process = self.spawn("import time; time.sleep(0.04)")
        self.control.attach(slow, slow_process, OWNER, actor_id="worker-a",
                            timeout_seconds=0.12)
        self.control.attach(fast, fast_process, OWNER, actor_id="worker-a")
        self.assertEqual(fast_process.wait(timeout=3), 0)
        self.assertIs(self.control.finish(fast, OWNER, success=True), TaskState.SUCCEEDED)
        self.delivery.report(fast, OWNER, actor_id="worker-a",
                             token=fast_ticket.token, success=True)
        self.wait_for_state(slow, TaskState.TIMED_OUT)
        self.assertFalse(TaskProcessController._group_running(slow_process.pid))
        self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
        self.restart()
        self.assertEqual(self.inbox.state(slow, OWNER), TaskState.TIMED_OUT)
        self.assertEqual(self.inbox.state(fast, OWNER), TaskState.SUCCEEDED)

    def test_sqlite_fault_after_stopping_process_keeps_task_accounted(self):
        task_id = self.create()
        self.claim(task_id)
        self.reserve(task_id)
        process = self.spawn()
        self.control.attach(task_id, process, OWNER, actor_id="worker-a")
        with patch.object(self.inbox._store, "write_transition",
                          side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                self.control.cancel(task_id, OWNER)
        self.assertFalse(TaskProcessController._group_running(process.pid))
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        pending = self.delivery.recovery_inventory(OWNER)
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].review_required)
        self.assertEqual(pending[0].delivery_status, "active")
        self.assertIs(self.control.cancel(task_id, OWNER), TaskState.CANCELLED)
        self.restart()
        self.assertEqual(self.delivery.recovery_inventory(OWNER), ())

    def test_untracked_running_without_delivery_is_never_invisible(self):
        task_id = self.create()
        self.claim(task_id)
        self.inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                    actor_id="worker-a")
        self.assertEqual(self.delivery.resume_pending(OWNER), ())
        self.restart()
        inventory = self.delivery.recovery_inventory(OWNER)
        self.assertEqual(len(inventory), 1)
        self.assertEqual((inventory[0].state, inventory[0].delivery_status),
                         (TaskState.RUNNING, None))
        self.assertTrue(inventory[0].review_required)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        self.assertFalse(self.delivery.recovery_inventory(FOREIGN))

    def test_recovery_inventory_scoped_queued_assigned_and_active(self):
        queued = self.create()
        assigned = self.create()
        running = self.create()
        foreign = self.create(scope=FOREIGN)
        self.claim(assigned, 0)
        self.claim(running, 1)
        self.reserve(running)
        self.inbox.transition_state(running, TaskState.RUNNING, OWNER,
                                    actor_id="worker-a")
        self.restart()
        entries = {item.task_id: item for item in
                   self.delivery.recovery_inventory(OWNER)}
        self.assertEqual(set(entries), {queued, assigned, running})
        self.assertEqual((entries[queued].state, entries[queued].review_required),
                         (TaskState.QUEUED, False))
        self.assertEqual((entries[assigned].state, entries[assigned].review_required),
                         (TaskState.ASSIGNED, True))
        self.assertEqual((entries[running].delivery_status,
                          entries[running].review_required), ("active", True))
        self.assertEqual({r.task_id for r in self.delivery.recovery_inventory(FOREIGN)},
                         {foreign})
        self.assertFalse(self.reserve(running).should_execute)

    def test_inventory_storage_error_refuses_partial_success(self):
        self.create()
        with patch.object(self.inbox._store, "delivery",
                          side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                self.delivery.recovery_inventory(OWNER)

    def test_permissions_survive_restart_as_audit_not_grants(self):
        task_id = self.create()
        self.claim(task_id)
        ticket = self.reserve(task_id)
        action = ProtectedAction.WRITE_WORKSPACE
        gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(task_id, "worker-a", action),))
        self.control = TaskProcessController(self.inbox, permission_gate=gate)
        process = self.spawn()
        self.control.attach(task_id, process, OWNER, actor_id="worker-a")
        marker = self.base / "allowed.txt"
        result = self.control.execute_protected(task_id, OWNER, actor_id="worker-a",
                                                 action=action,
                                                 operation=lambda: marker.write_text("ok"))
        self.assertEqual(result, 2)
        with self.assertRaises(PermissionDenied):
            self.control.execute_protected(task_id, OWNER, actor_id="worker-a",
                                           action=ProtectedAction.DELETE_ARTIFACT,
                                           operation=lambda: marker.unlink())
        self.assertEqual(marker.read_text(), "ok")
        self.assertIs(self.control.cancel(task_id, OWNER), TaskState.CANCELLED)
        self.restart()
        self.assertEqual([r.allowed for r in
                          TaskPermissionGate(self.inbox).audit_history(task_id, OWNER)],
                         [True, False])
        self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
        self.assertIsNotNone(ticket.token)

    def test_sqlite_disposable_restore_has_complete_terminal_history(self):
        task_id = self.create()
        self.claim(task_id)
        ticket = self.reserve(task_id)
        process = self.spawn("pass")
        self.control.attach(task_id, process, OWNER, actor_id="worker-a")
        self.assertEqual(process.wait(timeout=3), 0)
        self.control.finish(task_id, OWNER, success=True)
        self.delivery.report(task_id, OWNER, actor_id="worker-a",
                             token=ticket.token, success=True)
        self.inbox.close()
        restored = self.base / "restored.sqlite3"
        shutil.copy2(self.db, restored)
        with sqlite3.connect(restored) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(),
                             ("ok",))
        duplicate = TaskInbox(store=SQLiteTaskStore(restored), workspace_root=self.root)
        try:
            self.assertIs(duplicate.state(task_id, OWNER), TaskState.SUCCEEDED)
            self.assertTrue(duplicate.transition_history(task_id, OWNER))
            self.assertEqual(TaskDeliveryManager(duplicate).recovery_inventory(OWNER), ())
        finally:
            duplicate.close()
        # Reopen the fixture for tearDown.
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.delivery = TaskDeliveryManager(self.inbox)
        self.control = TaskProcessController(self.inbox)

    def test_forced_supervisor_crash_is_flagged_and_never_auto_redelivered(self):
        """Actually SIGKILL a separate supervisor with a still-live child.

        The harness is the trusted reconciler: it separately verifies and
        kills the orphan before changing task state. The engine does NOT
        claim it automatically reaps children after SIGKILL.
        """
        self.inbox.close()
        code = textwrap.dedent("""
            import json, subprocess, sys, time
            from pathlib import Path
            from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
            from ai_agent_engine.task_assignment import WorkspaceRoot
            from ai_agent_engine.task_control import TaskProcessController
            from ai_agent_engine.task_delivery import TaskDeliveryManager
            from ai_agent_engine.task_storage import SQLiteTaskStore
            owner = OwnerScope('owner-a', frozenset({'project-a'}))
            inbox = TaskInbox(store=SQLiteTaskStore(Path(sys.argv[1])),
                              workspace_root=WorkspaceRoot(Path(sys.argv[2])))
            job = parse_submission(json.dumps({'project_id':'project-a',
                'title':'crash test','prompt':'inert'}).encode())
            task = inbox.accept(job, owner, idempotency_key='crash-once').task_id
            inbox.claim_task(task, 'worker-a', 'slot-00', owner)
            ticket = TaskDeliveryManager(inbox).reserve(
                task, owner, actor_id='worker-a', max_attempts=1)
            child = subprocess.Popen([sys.executable, '-c',
                'import time;time.sleep(60)'], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            TaskProcessController(inbox).attach(task, child, owner, actor_id='worker-a')
            print(json.dumps({'task':task, 'pid':child.pid, 'token':ticket.token}),
                  flush=True)
            time.sleep(60)
        """)
        supervisor = subprocess.Popen([sys.executable, "-u", "-c", code,
                                       str(self.db), str(self.root.path)],
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(supervisor.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5),
                                "isolated supervisor never became ready")
            line = supervisor.stdout.readline()
            self.assertTrue(line.startswith("{"), "isolated supervisor did not start")
            record = json.loads(line)
            pid = record["pid"]
            self.orphans.append(pid)
            supervisor.kill()
            supervisor.wait(timeout=3)
            self.assertTrue(TaskProcessController._group_running(pid))
            self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                                   workspace_root=self.root)
            self.delivery = TaskDeliveryManager(self.inbox, retry_delay_seconds=0)
            self.control = TaskProcessController(self.inbox)
            inventory = self.delivery.recovery_inventory(OWNER)
            self.assertEqual([(r.task_id, r.state, r.delivery_status,
                               r.review_required) for r in inventory],
                             [(record["task"], TaskState.RUNNING, "active", True)])
            self.assertFalse(self.reserve(record["task"], max_attempts=1).should_execute)
            with self.assertRaises(WorkspaceBusy):
                self.root.acquire("slot-00")
            # Deliberate trusted reconciliation. No automatic retry or
            # unverifiable 'completed' claim is allowed on crash.
            os.killpg(pid, signal.SIGKILL)
            until = time.monotonic() + 3
            while TaskProcessController._group_running(pid) and time.monotonic() < until:
                time.sleep(0.01)
            self.assertFalse(TaskProcessController._group_running(pid))
            self.assertEqual(self.delivery.report(
                record["task"], OWNER, actor_id="worker-a",
                token=record["token"], success=False).status, "exhausted")
            self.inbox.transition_state(record["task"], TaskState.FAILED, OWNER,
                                        actor_id="worker-a")
            self.assertEqual(self.delivery.recovery_inventory(OWNER), ())
            self.restart()
            self.assertIs(self.inbox.state(record["task"], OWNER), TaskState.FAILED)
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=3)
            supervisor.stdout.close()
            supervisor.stderr.close()
