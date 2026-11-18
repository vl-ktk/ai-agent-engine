"""Stage 5.13: dangerous actions are refused and durably audited sans secrets."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
from threading import Barrier, Thread
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import OwnerScope, TaskInbox, TaskNotFound, parse_submission, task_handler
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_control import TaskControlError, TaskProcessController
from ai_agent_engine.task_permissions import (
    ActionGrant, PermissionDenied, PermissionUnavailable, ProtectedAction,
    TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("owner-b", frozenset({"project-a"}))


def submission():
    return parse_submission(json.dumps({
        "project_id": "project-a", "title": "SENSITIVE TITLE",
        "prompt": "TOP SECRET PROMPT: NEVER AUDIT THIS",
    }).encode())


class TaskPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-513-")
        base = Path(self.temp.name)
        self.workspaces = base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        for name in ("slot-a", "slot-b"):
            (self.workspaces / name).mkdir(mode=0o700)
        self.root = WorkspaceRoot(self.workspaces)
        self.path = base / "task.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.gate = TaskPermissionGate(self.inbox)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def create(self, scope=OWNER):
        return self.inbox.accept(submission(), scope).task_id

    def running(self, *, scope=OWNER, slot="slot-a"):
        task_id = self.create(scope)
        self.inbox.claim_task(task_id, "worker-a", slot, scope)
        self.inbox.transition_state(task_id, TaskState.RUNNING, scope,
                                    actor_id="worker-a")
        return task_id

    def grant(self, task_id, action=ProtectedAction.WRITE_WORKSPACE,
              *, expires_at=None, worker="worker-a"):
        self.gate = TaskPermissionGate(self.inbox, grants=(ActionGrant(
            task_id, worker, action, expires_at),))

    def perform(self, task_id, action, fn, *, actor="worker-a", scope=OWNER):
        return self.gate.run(task_id, scope, actor_id=actor,
                             action=action, operation=fn)

    def audit(self, task_id, scope=OWNER):
        return self.gate.audit_history(task_id, scope)

    def restart(self):
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.gate = TaskPermissionGate(self.inbox)

    def test_dangerous_write_default_denied_before_side_effect_and_audited(self):
        task_id = self.running()
        marker = self.workspaces / "slot-a" / "forbidden"
        with self.assertRaises(PermissionDenied):
            self.perform(task_id, ProtectedAction.WRITE_WORKSPACE,
                     lambda: marker.write_text("DANGEROUS WRITE"))
        self.assertFalse(marker.exists())
        events = self.audit(task_id)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].allowed, events[0].reason_code),
                         (False, "grant_missing"))
        self.assertEqual(events[0].action, ProtectedAction.WRITE_WORKSPACE)
        self.restart()
        self.assertEqual(len(self.audit(task_id)), 1)

    def test_exact_grant_allows_only_requested_action_and_records_decision(self):
        task_id = self.running()
        self.grant(task_id)
        marker = self.workspaces / "slot-a" / "authorized"
        self.assertEqual(self.perform(task_id, ProtectedAction.WRITE_WORKSPACE,
                                  lambda: marker.write_text("yes")), 3)
        self.assertEqual(marker.read_text(), "yes")
        self.assertEqual([(e.allowed, e.reason_code) for e in self.audit(task_id)],
                         [(True, "granted")])
        with self.assertRaises(PermissionDenied):
            self.perform(task_id, ProtectedAction.READ_SECRET, lambda: "secret")
        self.assertEqual(self.audit(task_id)[-1].reason_code, "grant_missing")

    def test_grant_does_not_authorize_task_before_running(self):
        task_id = self.create()
        self.inbox.claim_task(task_id, "worker-a", "slot-a", OWNER)
        self.grant(task_id, ProtectedAction.EXECUTE_TOOL)
        invoked = []
        with self.assertRaises(PermissionDenied):
            self.perform(task_id, ProtectedAction.EXECUTE_TOOL, lambda: invoked.append(1))
        self.assertEqual(invoked, [])
        self.assertEqual(self.audit(task_id)[0].reason_code, "task_not_running")

    def test_worker_mismatch_denies_without_exposing_actual_worker_id(self):
        task_id = self.running()
        self.grant(task_id)
        invoked = []
        with self.assertRaises(PermissionDenied) as denied:
            self.perform(task_id, ProtectedAction.WRITE_WORKSPACE,
                     lambda: invoked.append(True), actor="another-worker")
        self.assertEqual(str(denied.exception), "permission_denied")
        self.assertEqual(invoked, [])
        event = self.audit(task_id)[0]
        self.assertEqual((event.reason_code, event.principal_kind),
                         ("worker_mismatch", "other_worker"))
        self.assertNotIn("worker-a", repr(event))

    def test_grant_cannot_be_used_for_another_task_or_owner(self):
        first = self.running()
        second = self.running(slot="slot-b")
        self.grant(first)
        with self.assertRaises(PermissionDenied):
            self.perform(second, ProtectedAction.WRITE_WORKSPACE, lambda: True)
        self.assertEqual(self.audit(second)[0].reason_code, "grant_missing")
        with self.assertRaises(TaskNotFound):
            self.perform(first, ProtectedAction.WRITE_WORKSPACE, lambda: True,
                     scope=FOREIGN)
        with self.assertRaises(TaskNotFound):
            self.audit(first, FOREIGN)
        self.assertEqual(len(self.audit(first)), 0)

    def test_expired_grant_denied_and_logged(self):
        task_id = self.running()
        self.grant(task_id, expires_at=time.time() - 10)
        with self.assertRaises(PermissionDenied):
            self.perform(task_id, ProtectedAction.WRITE_WORKSPACE, lambda: True)
        self.assertEqual(self.audit(task_id)[0].reason_code, "grant_expired")

    def test_future_grant_valid_then_expired_on_next_check(self):
        task_id = self.running()
        deadline = time.time() + 300
        self.grant(task_id, expires_at=deadline)
        self.assertEqual(self.perform(task_id, ProtectedAction.WRITE_WORKSPACE,
                                  lambda: "done"), "done")
        with patch("ai_agent_engine.task_permissions.time.time", return_value=deadline + 1):
            with self.assertRaises(PermissionDenied):
                self.perform(task_id, ProtectedAction.WRITE_WORKSPACE, lambda: "bad")
        self.assertEqual([e.reason_code for e in self.audit(task_id)],
                         ["granted", "grant_expired"])

    def test_storage_failure_fails_closed_before_allowed_operation(self):
        task_id = self.running()
        self.grant(task_id)
        marker = self.workspaces / "slot-a" / "must-not-exist"
        with patch.object(self.inbox._store, "write_permission_audit",
                          side_effect=StorageUnavailable("PRIVATE PATH")):
            with self.assertRaises(StorageUnavailable):
                self.perform(task_id, ProtectedAction.WRITE_WORKSPACE,
                         lambda: marker.write_text("oops"))
        self.assertFalse(marker.exists())
        self.assertEqual(self.audit(task_id), ())

    def test_database_trigger_failure_is_fail_closed(self):
        task_id = self.running()
        self.grant(task_id, ProtectedAction.READ_SECRET)
        with self.inbox._store._db() as db:
            db.execute("""CREATE TRIGGER reject_permission_audit
                BEFORE INSERT ON task_permission_audit
                BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
        invoked = []
        with self.assertRaises(StorageUnavailable):
            self.perform(task_id, ProtectedAction.READ_SECRET,
                     lambda: invoked.append("secret"))
        self.assertEqual(invoked, [])
        self.assertEqual(self.audit(task_id), ())

    def test_audit_schema_contains_no_prompt_paths_secrets_or_worker_ids(self):
        task_id = self.running()
        self.grant(task_id, ProtectedAction.READ_SECRET)
        self.perform(task_id, ProtectedAction.READ_SECRET, lambda: "PRIVATE OUTPUT")
        with sqlite3.connect(self.path) as db:
            columns = [row[1] for row in db.execute(
                "PRAGMA table_info(task_permission_audit)").fetchall()]
            row = db.execute("SELECT * FROM task_permission_audit").fetchone()
        raw = str(row) + " ".join(columns)
        for forbidden in ("prompt", "title", "worker_id", "owner_id",
                          "PRIVATE OUTPUT", "TOP SECRET PROMPT", "SENSITIVE TITLE",
                          "worker-a", "owner-a", str(self.workspaces)):
            self.assertNotIn(forbidden, raw)
        self.assertEqual(len(row), 7)

    def test_callback_exception_does_not_claim_the_action_succeeded(self):
        task_id = self.running()
        self.grant(task_id, ProtectedAction.EXECUTE_TOOL)

        def explode():
            raise ValueError("SECRET HANDLER FAILURE")

        with self.assertRaises(ValueError):
            self.perform(task_id, ProtectedAction.EXECUTE_TOOL, explode)
        self.assertEqual([(e.allowed, e.reason_code) for e in self.audit(task_id)],
                         [(True, "granted")])
        self.assertNotIn("HANDLER FAILURE", str(self.audit(task_id)))

    def test_rejected_audit_events_survive_restart_in_order(self):
        task_id = self.running()
        for action in (ProtectedAction.READ_SECRET,
                       ProtectedAction.DELETE_ARTIFACT,
                       ProtectedAction.EXTERNAL_REQUEST):
            with self.assertRaises(PermissionDenied):
                self.perform(task_id, action, lambda: True)
        before = self.audit(task_id)
        self.restart()
        self.assertEqual(self.audit(task_id), before)
        self.assertEqual([e.event_id for e in before], sorted(e.event_id for e in before))

    def test_32_concurrent_denials_all_audited_and_no_operations_run(self):
        task_id = self.running()
        invoked = []

        def attempt(_):
            try:
                self.perform(task_id, ProtectedAction.DELETE_ARTIFACT,
                         lambda: invoked.append("oops"))
            except PermissionDenied:
                return "denied"
            return "wrong"

        with ThreadPoolExecutor(max_workers=16) as pool:
            result = list(pool.map(attempt, range(32)))
        self.assertEqual(result, ["denied"] * 32)
        self.assertEqual(invoked, [])
        self.assertEqual(len(self.audit(task_id)), 32)
        self.restart()
        self.assertEqual(len(self.audit(task_id)), 32)

    def test_parallel_authorized_tasks_do_not_hold_global_execution_lock(self):
        first = self.running()
        second = self.running(slot="slot-b")
        self.gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(first, "worker-a", ProtectedAction.EXECUTE_TOOL),
            ActionGrant(second, "worker-a", ProtectedAction.EXECUTE_TOOL),
        ))
        barrier = Barrier(2)

        def operation(task_id):
            return self.perform(task_id, ProtectedAction.EXECUTE_TOOL,
                            lambda: barrier.wait(timeout=3))

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(operation, (first, second)))
        self.assertEqual(len(result), 2)
        self.assertTrue(all(self.audit(task_id)[0].allowed for task_id
                            in (first, second)))

    def test_no_ram_only_gate_can_silently_skip_audit(self):
        inbox = TaskInbox()
        try:
            with self.assertRaises(PermissionUnavailable):
                TaskPermissionGate(inbox)
        finally:
            inbox.close()

    def test_unknown_action_and_uncallable_operation_are_never_executed(self):
        task_id = self.running()
        self.grant(task_id)
        with self.assertRaises(TypeError):
            self.perform(task_id, "write_workspace", lambda: True)
        with self.assertRaises(TypeError):
            self.perform(task_id, ProtectedAction.WRITE_WORKSPACE, "raw shell command")
        self.assertEqual(self.audit(task_id), ())

    def test_invalid_or_duplicate_grants_are_rejected(self):
        task_id = self.running()
        with self.assertRaises(ValueError):
            ActionGrant("../invalid", "worker-a", ProtectedAction.READ_SECRET)
        with self.assertRaises(TypeError):
            ActionGrant(task_id, "worker-a", "read_secret")
        with self.assertRaises(ValueError):
            ActionGrant(task_id, "worker-a", ProtectedAction.READ_SECRET,
                        expires_at=-1)
        grant = ActionGrant(task_id, "worker-a", ProtectedAction.READ_SECRET)
        with self.assertRaises(ValueError):
            TaskPermissionGate(self.inbox, grants=(grant, grant))
        self.assertEqual(self.audit(task_id), ())

    def test_grant_policy_is_immutable_and_cannot_be_added_via_prompt(self):
        task_id = self.running()
        self.grant(task_id, ProtectedAction.WRITE_WORKSPACE)
        with self.assertRaises(TypeError):
            self.gate._grants[(task_id, "worker-a", ProtectedAction.READ_SECRET)] = None
        with self.assertRaises(PermissionDenied):
            self.perform(task_id, ProtectedAction.READ_SECRET, lambda: "unsafe")

    def test_terminal_task_cannot_use_stale_grant(self):
        task_id = self.running()
        self.grant(task_id)
        self.inbox.transition_state(task_id, TaskState.SUCCEEDED, OWNER,
                                    actor_id="worker-a")
        with self.assertRaises(PermissionDenied):
            self.perform(task_id, ProtectedAction.WRITE_WORKSPACE, lambda: True)
        self.assertEqual(self.audit(task_id)[0].reason_code, "task_not_running")

    def test_v4_to_v5_migration_keeps_tasks_and_delivery_ledger(self):
        task_id = self.running()
        self.inbox._store.begin_delivery(task_id, 2, "a" * 32, time.time())
        self.inbox.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE task_provider_audit")
            db.execute("DROP TABLE task_permission_audit")
            db.execute("PRAGMA user_version=4")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.gate = TaskPermissionGate(self.inbox)
        self.assertEqual(self.inbox._store._db().execute("PRAGMA user_version").fetchone(),
                             (8,))
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        self.assertEqual(self.inbox._store.delivery(task_id)[1], 1)
        with self.assertRaises(PermissionDenied):
            self.perform(task_id, ProtectedAction.EXECUTE_TOOL, lambda: True)
        self.assertEqual(self.audit(task_id)[0].reason_code, "grant_missing")

    def test_no_http_endpoint_can_grant_or_invoke_a_dangerous_action(self):
        task_id = self.running()
        self.grant(task_id, ProtectedAction.EXECUTE_TOOL)
        server = ThreadingHTTPServer(("127.0.0.1", 0), task_handler(self.inbox, OWNER))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                              timeout=3)
            try:
                body = json.dumps({"action": "execute_tool", "allowed": True}).encode()
                conn.request("POST", f"/v1/tasks/{task_id}/actions", body,
                             {"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                self.assertEqual(json.loads(response.read())["status"], "not_found")
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(self.audit(task_id), ())

    def test_real_managed_process_cannot_execute_denied_action(self):
        task_id = self.create()
        self.inbox.claim_task(task_id, "worker-a", "slot-a", OWNER)
        gate = TaskPermissionGate(self.inbox)
        control = TaskProcessController(self.inbox, permission_gate=gate)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        marker = self.workspaces / "slot-a" / "forbidden-by-controller"
        try:
            control.attach(task_id, process, OWNER, actor_id="worker-a")
            with self.assertRaises(PermissionDenied):
                control.execute_protected(
                    task_id, OWNER, actor_id="worker-a",
                    action=ProtectedAction.DELETE_ARTIFACT,
                    operation=lambda: marker.write_text("delete something"))
            self.assertFalse(marker.exists())
            self.assertIsNone(process.poll())
            self.assertEqual(gate.audit_history(task_id, OWNER)[0].reason_code,
                             "grant_missing")
        finally:
            control.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)

    def test_real_managed_process_performs_only_explicit_granted_action(self):
        task_id = self.create()
        self.inbox.claim_task(task_id, "worker-a", "slot-a", OWNER)
        gate = TaskPermissionGate(self.inbox, grants=(ActionGrant(
            task_id, "worker-a", ProtectedAction.WRITE_WORKSPACE),))
        control = TaskProcessController(self.inbox, permission_gate=gate)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        marker = self.workspaces / "slot-a" / "allowed-by-controller"
        try:
            control.attach(task_id, process, OWNER, actor_id="worker-a")
            control.execute_protected(task_id, OWNER, actor_id="worker-a",
                                      action=ProtectedAction.WRITE_WORKSPACE,
                                      operation=lambda: marker.write_text("okay"))
            self.assertEqual(marker.read_text(), "okay")
            with self.assertRaises(PermissionDenied):
                control.execute_protected(task_id, OWNER, actor_id="worker-a",
                                          action=ProtectedAction.READ_SECRET,
                                          operation=lambda: "secret")
            self.assertEqual([e.reason_code for e in gate.audit_history(task_id, OWNER)],
                             ["granted", "grant_missing"])
        finally:
            control.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)

    def test_managed_controller_without_gate_refuses_protected_operation(self):
        task_id = self.create()
        self.inbox.claim_task(task_id, "worker-a", "slot-a", OWNER)
        control = TaskProcessController(self.inbox)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            control.attach(task_id, process, OWNER, actor_id="worker-a")
            with self.assertRaises(PermissionUnavailable):
                control.execute_protected(task_id, OWNER, actor_id="worker-a",
                                          action=ProtectedAction.EXECUTE_TOOL,
                                          operation=lambda: "not allowed")
            self.assertIsNone(process.poll())
        finally:
            control.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
