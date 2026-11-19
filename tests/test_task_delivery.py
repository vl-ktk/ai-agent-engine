"""Stage 5.12: real durable duplicate suppression, limited retries and resume."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import (
    IdempotencyConflict, InboxFull, InvalidTask, OwnerScope, TaskInbox,
    TaskNotFound, parse_submission,
    task_handler,
)
from ai_agent_engine.task_assignment import WorkspaceBusy, WorkspaceRoot
from ai_agent_engine.task_control import TaskControlError, TaskProcessController
from ai_agent_engine.task_delivery import DeliveryRejected, TaskDeliveryManager
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a", "project-b"}))
OTHER = OwnerScope("owner-b", frozenset({"project-a"}))


def submission(*, title="Test job", project="project-a"):
    return parse_submission(json.dumps({
        "project_id": project, "title": title, "prompt": "PRIVATE PROMPT",
    }).encode())


class TaskDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-512-")
        base = Path(self.temp.name)
        ws = base / "workspaces"
        ws.mkdir(mode=0o700)
        for name in ("slot-a", "slot-b"):
            (ws / name).mkdir(mode=0o700)
        self.root = WorkspaceRoot(ws)
        self.path = base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.manager = TaskDeliveryManager(self.inbox, retry_delay_seconds=0)
        self.control = TaskProcessController(self.inbox)
        self.processes = []

    def tearDown(self):
        try:
            self.control.close()
        finally:
            for process in self.processes:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=3)
            self.inbox.close()
            self.temp.cleanup()

    def create(self, scope=OWNER, *, title="Test job", project="project-a",
               key=None):
        return self.inbox.accept(submission(title=title, project=project), scope,
                                 idempotency_key=key).task_id

    def claim(self, task_id, workspace="slot-a", scope=OWNER):
        return self.inbox.claim_task(task_id, "worker-a", workspace, scope)

    def reserve(self, task_id, *, scope=OWNER, max_attempts=3):
        return self.manager.reserve(task_id, scope, actor_id="worker-a",
                                    max_attempts=max_attempts)

    def report(self, task_id, token, success, *, scope=OWNER):
        return self.manager.report(task_id, scope, actor_id="worker-a",
                                   token=token, success=success)

    def restart(self):
        self.control.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.manager = TaskDeliveryManager(self.inbox, retry_delay_seconds=0)
        self.control = TaskProcessController(self.inbox)

    def spawn(self, code):
        p = subprocess.Popen([sys.executable, "-c", code],
                             start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.processes.append(p)
        return p

    def test_idempotent_admission_one_task_id_and_receipt_after_restart(self):
        first = self.create(key="request-1")
        self.assertEqual(first, self.create(key="request-1"))
        self.assertEqual(len(self.inbox.snapshot(OWNER)), 1)
        self.restart()
        self.assertEqual(first, self.create(key="request-1"))
        self.assertEqual(len(self.inbox.snapshot(OWNER)), 1)
        self.assertEqual(self.inbox.get(first, OWNER).receipt, 1)

    def test_idempotency_replay_when_queue_is_full(self):
        self.inbox.close()
        self.inbox = TaskInbox(max_pending=1,
                               store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.manager = TaskDeliveryManager(self.inbox)
        self.control = TaskProcessController(self.inbox)
        first = self.create(key="one")
        self.assertEqual(first, self.create(key="one"))
        with self.assertRaises(InboxFull):
            self.create(key="two")

    def test_idempotency_conflict_does_not_create_second_task(self):
        first = self.create(key="same-key")
        with self.assertRaises(IdempotencyConflict):
            self.create(key="same-key", title="Different job")
        self.assertEqual(len(self.inbox.snapshot(OWNER)), 1)
        self.restart()
        with self.assertRaises(IdempotencyConflict):
            self.create(key="same-key", title="Different job")
        self.assertIsNotNone(self.inbox.get(first, OWNER))

    def test_idempotency_keys_scoped_by_owner_and_project(self):
        first = self.create(key="same")
        second = self.create(scope=OTHER, key="same")
        third = self.create(project="project-b", key="same")
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(self.create(scope=OTHER, key="same"), second)
        self.assertEqual(self.create(project="project-b", key="same"), third)

    def test_concurrent_keyed_accept_has_only_one_writer_and_no_second_receipt(self):
        with ThreadPoolExecutor(max_workers=16) as pool:
            ids = list(pool.map(lambda _: self.create(key="fan-in"), range(32)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.inbox.snapshot(OWNER)), 1)
        self.assertEqual(self.inbox.get(self.create(key="different"), OWNER).receipt, 2)

    def test_key_and_payload_never_written_in_raw_form_to_ledger(self):
        self.create(key="very-secret-client-key")
        with sqlite3.connect(self.path) as db:
            key_hash, payload_hash = db.execute("""SELECT key_hash,payload_hash
                FROM task_admission_keys""").fetchone()
        self.assertEqual(len(key_hash), 64)
        self.assertEqual(len(payload_hash), 64)
        self.assertNotIn("very-secret-client-key", key_hash + payload_hash)
        self.assertNotIn("PRIVATE PROMPT", key_hash + payload_hash)

    def test_invalid_idempotency_key_does_not_admit(self):
        for bad in ("", "bad key", "../bad", "*", "x" * 129, 7, True):
            with self.subTest(bad=bad), self.assertRaises(InvalidTask):
                self.create(key=bad)
        self.assertEqual(self.inbox.snapshot(OWNER), ())

    def test_failed_atomic_admission_can_retry_same_key(self):
        with patch.object(self.inbox._store, "insert_task",
                          side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                self.create(key="save-fails")
        self.assertEqual(self.inbox.snapshot(OWNER), ())
        self.create(key="save-fails")
        self.assertEqual(len(self.inbox.snapshot(OWNER)), 1)

    def test_v3_to_v4_upgrade_preserves_task_and_assignment(self):
        task_id = self.create(key=None)
        self.claim(task_id)
        self.inbox.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE task_provider_audit")
            db.execute("DROP TABLE task_permission_audit")
            db.execute("DROP TABLE task_deliveries")
            db.execute("DROP TABLE task_admission_keys")
            db.execute("PRAGMA user_version=3")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.manager = TaskDeliveryManager(self.inbox)
        self.control = TaskProcessController(self.inbox)
        self.assertEqual(self.inbox._store._db().execute("PRAGMA user_version").fetchone(),
                             (14,))
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)
        self.assertTrue(self.reserve(task_id).should_execute)

    def test_duplicate_delivery_never_gets_an_executable_token(self):
        task_id = self.create()
        self.claim(task_id)
        first = self.reserve(task_id)
        self.assertTrue(first.should_execute)
        self.assertEqual(len(first.token), 32)
        for _ in range(8):
            replay = self.reserve(task_id)
            self.assertFalse(replay.should_execute)
            self.assertIsNone(replay.token)
            self.assertEqual(replay.attempt, 1)
        self.assertEqual(self.manager.snapshot(task_id, OWNER).status, "active")

    def test_concurrent_delivery_32_requests_only_one_execution(self):
        task_id = self.create()
        self.claim(task_id)
        with ThreadPoolExecutor(max_workers=16) as pool:
            tickets = list(pool.map(lambda _: self.reserve(task_id), range(32)))
        self.assertEqual(sum(t.should_execute for t in tickets), 1)
        self.assertEqual(sum(t.token is not None for t in tickets), 1)

    def test_crash_resume_never_reissues_uncertain_active_attempt(self):
        task_id = self.create()
        self.claim(task_id)
        ticket = self.reserve(task_id)
        self.inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                    actor_id="worker-a")
        self.restart()
        snapshot = self.manager.snapshot(task_id, OWNER)
        self.assertEqual((snapshot.status, snapshot.attempt), ("active", 1))
        pending = self.manager.resume_pending(OWNER)
        self.assertEqual([s.task_id for s in pending], [task_id])
        self.assertFalse(self.reserve(task_id).should_execute)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        # Worker may later deliver the saved token; retry is only then unlocked.
        self.assertEqual(self.report(task_id, ticket.token, False).status, "retryable")
        self.assertEqual(self.reserve(task_id).attempt, 2)

    def test_failed_attempt_retries_up_to_limit_and_then_exhausts(self):
        task_id = self.create()
        self.claim(task_id)
        tokens = []
        for attempt in (1, 2, 3):
            ticket = self.reserve(task_id)
            self.assertTrue(ticket.should_execute)
            self.assertEqual(ticket.attempt, attempt)
            tokens.append(ticket.token)
            snapshot = self.report(task_id, ticket.token, False)
            self.assertEqual(snapshot.status,
                             "exhausted" if attempt == 3 else "retryable")
        self.assertEqual(len(set(tokens)), 3)
        self.assertFalse(self.reserve(task_id).should_execute)
        self.assertEqual(self.manager.snapshot(task_id, OWNER).attempt, 3)
        self.restart()
        self.assertEqual(self.manager.snapshot(task_id, OWNER).status, "exhausted")

    def test_success_ack_is_idempotent_and_conflict_is_rejected(self):
        task_id = self.create()
        self.claim(task_id)
        ticket = self.reserve(task_id)
        self.inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                    actor_id="worker-a")
        self.inbox.transition_state(task_id, TaskState.SUCCEEDED, OWNER,
                                    actor_id="worker-a")
        first = self.report(task_id, ticket.token, True)
        repeated = self.report(task_id, ticket.token, True)
        self.assertEqual(first, repeated)
        self.assertEqual(first.status, "completed")
        self.assertFalse(self.reserve(task_id).should_execute)
        with self.assertRaises(DeliveryRejected):
            self.report(task_id, ticket.token, False)

    def test_superseded_token_is_rejected_without_consuming_retry(self):
        task_id = self.create()
        self.claim(task_id)
        first = self.reserve(task_id)
        self.report(task_id, first.token, False)
        second = self.reserve(task_id)
        with self.assertRaises(DeliveryRejected):
            self.report(task_id, first.token, False)
        self.assertEqual(self.manager.snapshot(task_id, OWNER).attempt, 2)
        self.inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                    actor_id="worker-a")
        self.inbox.transition_state(task_id, TaskState.SUCCEEDED, OWNER,
                                    actor_id="worker-a")
        self.report(task_id, second.token, True)

    def test_wrong_worker_owner_and_unassigned_task_are_rejected(self):
        task_id = self.create()
        with self.assertRaises(DeliveryRejected):
            self.reserve(task_id)
        self.claim(task_id)
        with self.assertRaises(TaskNotFound):
            self.reserve(task_id, scope=OTHER)
        with self.assertRaises(DeliveryRejected):
            self.manager.reserve(task_id, OWNER, actor_id="worker-b")
        self.assertIsNone(self.manager.snapshot(task_id, OWNER))

    def test_invalid_or_changed_policy_is_rejected(self):
        task_id = self.create()
        self.claim(task_id)
        for invalid in (0, 6, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.reserve(task_id, max_attempts=invalid)
        ticket = self.reserve(task_id, max_attempts=2)
        with self.assertRaises(DeliveryRejected):
            self.reserve(task_id, max_attempts=3)
        self.report(task_id, ticket.token, False)
        self.assertEqual(self.reserve(task_id, max_attempts=2).attempt, 2)

    def test_backoff_blocks_delivery_until_due(self):
        self.manager = TaskDeliveryManager(self.inbox, retry_delay_seconds=0.055)
        task_id = self.create()
        self.claim(task_id)
        first = self.reserve(task_id)
        waiting = self.report(task_id, first.token, False)
        self.assertGreater(waiting.retry_at, time.time())
        self.assertFalse(self.reserve(task_id).should_execute)
        time.sleep(0.07)
        self.assertTrue(self.reserve(task_id).should_execute)

    def test_terminal_task_and_missing_token_never_restart(self):
        task_id = self.create()
        self.claim(task_id)
        ticket = self.reserve(task_id)
        self.inbox.transition_state(task_id, TaskState.CANCELLED, OWNER,
                                    actor_id="orchestrator")
        with self.assertRaises(DeliveryRejected):
            self.reserve(task_id)
        with self.assertRaises(DeliveryRejected):
            self.report(task_id, ticket.token, True)

    def test_storage_failure_does_not_acknowledge_or_consume_attempt(self):
        task_id = self.create()
        self.claim(task_id)
        with patch.object(self.inbox._store, "begin_delivery",
                          side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                self.reserve(task_id)
        self.assertIsNone(self.manager.snapshot(task_id, OWNER))
        ticket = self.reserve(task_id)
        with patch.object(self.inbox._store, "settle_delivery",
                          side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                self.report(task_id, ticket.token, False)
        self.assertEqual(self.manager.snapshot(task_id, OWNER).status, "active")
        self.report(task_id, ticket.token, False)

    def test_recovery_snapshot_is_owner_scoped_and_has_no_tokens(self):
        own = self.create()
        other = self.create(scope=OTHER)
        self.claim(own)
        self.claim(other, "slot-b", OTHER)
        self.reserve(own)
        self.reserve(other, scope=OTHER)
        self.restart()
        self.assertEqual([s.task_id for s in self.manager.resume_pending(OWNER)], [own])
        self.assertEqual([s.task_id for s in self.manager.resume_pending(OTHER)], [other])
        self.assertNotIn("token", repr(self.manager.snapshot(own, OWNER)))

    def test_no_ram_only_manager_can_claim_durable_resume(self):
        inbox = TaskInbox()
        try:
            with self.assertRaises(DeliveryRejected):
                TaskDeliveryManager(inbox)
        finally:
            inbox.close()

    def test_two_local_process_attempts_same_task_no_duplicate_running_lease(self):
        first, other = self.create(), self.create()
        self.claim(first)
        ticket = self.reserve(first, max_attempts=2)
        p1 = self.spawn("import time; time.sleep(0.06); raise SystemExit(7)")
        self.control.attach(first, p1, OWNER, actor_id="worker-a")
        p1.wait(timeout=2)
        self.assertEqual(self.control.complete_failed_attempt(first, OWNER), 7)
        self.assertEqual(self.report(first, ticket.token, False).status, "retryable")
        with self.assertRaises(WorkspaceBusy):
            self.claim(other)
        retry = self.reserve(first, max_attempts=2)
        self.assertTrue(retry.should_execute)
        p2 = self.spawn("import time; time.sleep(0.06)")
        self.control.attach_retry(first, p2, OWNER, actor_id="worker-a",
                                  delivery_token=retry.token)
        p2.wait(timeout=2)
        self.assertIs(self.control.finish(first, OWNER, success=True),
                      TaskState.SUCCEEDED)
        self.assertEqual(self.report(first, retry.token, True).status, "completed")
        self.claim(other)
        self.restart()
        self.assertIs(self.inbox.state(first, OWNER), TaskState.SUCCEEDED)
        self.assertEqual(self.manager.snapshot(first, OWNER).status, "completed")
        self.assertEqual([e.to_state for e in self.inbox.transition_history(first, OWNER)
                          if e.accepted],
                         [TaskState.ASSIGNED, TaskState.RUNNING, TaskState.SUCCEEDED])

    def test_retry_attach_requires_real_ticket_and_exited_previous_process(self):
        task_id = self.create()
        self.claim(task_id)
        first = self.reserve(task_id)
        p1 = self.spawn("import time; time.sleep(0.12); raise SystemExit(2)")
        self.control.attach(task_id, p1, OWNER, actor_id="worker-a")
        next_process = self.spawn("import time; time.sleep(60)")
        with self.assertRaises(TaskControlError):
            self.control.attach_retry(task_id, next_process, OWNER,
                                      actor_id="worker-a", delivery_token=first.token)
        p1.wait(timeout=2)
        self.control.complete_failed_attempt(task_id, OWNER)
        self.report(task_id, first.token, False)
        second = self.reserve(task_id)
        with self.assertRaises(TaskControlError):
            self.control.attach_retry(task_id, next_process, OWNER,
                                      actor_id="worker-a", delivery_token=first.token)
        self.control.attach_retry(task_id, next_process, OWNER,
                                  actor_id="worker-a", delivery_token=second.token)
        self.control.cancel(task_id, OWNER)

    def test_exhausted_real_process_attempts_mark_failed_and_release_workspace(self):
        first, next_task = self.create(), self.create()
        self.claim(first)
        ticket = self.reserve(first, max_attempts=2)
        p1 = self.spawn("import time; time.sleep(0.06); raise SystemExit(2)")
        self.control.attach(first, p1, OWNER, actor_id="worker-a")
        p1.wait(timeout=2)
        self.control.complete_failed_attempt(first, OWNER)
        self.report(first, ticket.token, False)
        with self.assertRaises(TaskControlError):
            self.control.finalize_exhausted(first, OWNER)
        retry = self.reserve(first, max_attempts=2)
        p2 = self.spawn("import time; time.sleep(0.06); raise SystemExit(5)")
        self.control.attach_retry(first, p2, OWNER, actor_id="worker-a",
                                  delivery_token=retry.token)
        p2.wait(timeout=2)
        self.control.complete_failed_attempt(first, OWNER)
        self.assertEqual(self.report(first, retry.token, False).status, "exhausted")
        with self.assertRaises(WorkspaceBusy):
            self.claim(next_task)
        self.assertIs(self.control.finalize_exhausted(first, OWNER), TaskState.FAILED)
        self.claim(next_task)
        self.restart()
        self.assertIs(self.inbox.state(first, OWNER), TaskState.FAILED)
        self.assertEqual(self.manager.snapshot(first, OWNER).attempt, 2)

    def test_cancel_between_failed_attempts_does_not_leave_workspace_locked(self):
        first, next_task = self.create(), self.create()
        self.claim(first)
        ticket = self.reserve(first)
        process = self.spawn("import time; time.sleep(0.05); raise SystemExit(3)")
        self.control.attach(first, process, OWNER, actor_id="worker-a")
        process.wait(timeout=2)
        self.control.complete_failed_attempt(first, OWNER)
        self.report(first, ticket.token, False)
        self.assertIs(self.control.cancel(first, OWNER), TaskState.CANCELLED)
        self.claim(next_task)
        with self.assertRaises(DeliveryRejected):
            self.reserve(first)

    def test_no_duplicate_ticket_when_process_report_is_lost_before_restart(self):
        task_id = self.create(key="request-again")
        self.claim(task_id)
        ticket = self.reserve(task_id)
        self.restart()
        for _ in range(3):
            self.assertFalse(self.reserve(task_id).should_execute)
            self.assertEqual(task_id, self.create(key="request-again"))
        self.assertEqual(self.manager.snapshot(task_id, OWNER).attempt, 1)
        self.assertEqual(self.report(task_id, ticket.token, False).status, "retryable")
        self.assertTrue(self.reserve(task_id).should_execute)

    def test_repeated_delivery_creates_exactly_one_real_child_process(self):
        task_id = self.create(key="one-real-child")
        self.claim(task_id)
        with ThreadPoolExecutor(max_workers=12) as pool:
            tickets = list(pool.map(lambda _: self.reserve(task_id), range(32)))
        accepted = [t for t in tickets if t.should_execute]
        self.assertEqual(len(accepted), 1)
        marker = Path(self.temp.name) / "effects.txt"
        for ticket in tickets:
            if not ticket.should_execute:
                continue
            process = self.spawn("from pathlib import Path; import time; "
                                 f"Path({str(marker)!r}).write_text('one'); "
                                 "time.sleep(0.06)")
            self.control.attach(task_id, process, OWNER, actor_id="worker-a")
            process.wait(timeout=2)
            self.control.finish(task_id, OWNER, success=True)
            self.report(task_id, ticket.token, True)
        self.assertEqual(marker.read_text(), "one")
        self.assertEqual(self.manager.snapshot(task_id, OWNER).attempt, 1)
        self.restart()
        self.assertFalse(self.reserve(task_id).should_execute)

    def test_retryable_delivery_survives_restart_and_resumes_once(self):
        task_id = self.create()
        self.claim(task_id)
        first = self.reserve(task_id)
        self.report(task_id, first.token, False)
        self.restart()
        self.assertEqual(self.manager.resume_pending(OWNER)[0].status, "retryable")
        next_ticket = self.reserve(task_id)
        self.assertTrue(next_ticket.should_execute)
        self.assertEqual(next_ticket.attempt, 2)
        self.assertFalse(self.reserve(task_id).should_execute)

    def test_canceled_attempt_is_not_listed_as_pending_or_acknowledged(self):
        task_id = self.create()
        self.claim(task_id)
        ticket = self.reserve(task_id)
        self.inbox.transition_state(task_id, TaskState.CANCELLED, OWNER,
                                    actor_id="orchestrator")
        self.assertEqual(self.manager.resume_pending(OWNER), ())
        with self.assertRaises(DeliveryRejected):
            self.report(task_id, ticket.token, False)

    def test_success_ack_cannot_precede_verified_terminal_result(self):
        task_id = self.create()
        self.claim(task_id)
        ticket = self.reserve(task_id)
        with self.assertRaises(DeliveryRejected):
            self.report(task_id, ticket.token, True)
        self.assertEqual(self.manager.snapshot(task_id, OWNER).status, "active")
        self.inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                    actor_id="worker-a")
        with self.assertRaises(DeliveryRejected):
            self.report(task_id, ticket.token, True)
        self.assertEqual(self.manager.snapshot(task_id, OWNER).attempt, 1)

    def test_http_replayed_post_uses_same_task_id_and_payload_conflict_is_409(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), task_handler(self.inbox, OWNER))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(title, key):
                body = json.dumps({"project_id": "project-a", "title": title,
                                   "prompt": "PRIVATE PROMPT"}).encode()
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                                  timeout=3)
                try:
                    conn.request("POST", "/v1/tasks", body, {
                        "Content-Type": "application/json", "Idempotency-Key": key})
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    conn.close()
            first = post("Original", "request-123")
            replay = post("Original", "request-123")
            self.assertEqual(first[0], 202)
            self.assertEqual(replay[0], 202)
            self.assertEqual(first[1]["task_id"], replay[1]["task_id"])
            self.assertEqual(first[1]["receipt"], replay[1]["receipt"])
            conflict = post("Changed", "request-123")
            self.assertEqual(conflict[0], 409)
            self.assertEqual(conflict[1]["status"], "idempotency_conflict")
            invalid = post("Other", "../bad")
            self.assertEqual(invalid[0], 422)
            self.assertEqual(len(self.inbox.snapshot(OWNER)), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_12_parallel_http_posts_one_idempotent_task(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), task_handler(self.inbox, OWNER))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"project_id": "project-a", "title": "HTTP once",
                               "prompt": "PRIVATE PROMPT"}).encode()

            def post(_):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                                  timeout=5)
                try:
                    conn.request("POST", "/v1/tasks", body, {
                        "Content-Type": "application/json",
                        "Idempotency-Key": "concurrent-posts"})
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())["task_id"]
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(post, range(12)))
            self.assertEqual({status for status, _ in results}, {202})
            self.assertEqual(len({task_id for _, task_id in results}), 1)
            self.assertEqual(len(self.inbox.snapshot(OWNER)), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
