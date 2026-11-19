"""Stage 5.8: allowed and denied transitions with durable audit evidence."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import (
    OwnerScope, TaskInbox, TaskNotFound, parse_submission, task_handler,
)
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_state import (
    ALLOWED_TRANSITIONS, TaskState, TransitionRejected, can_transition,
)
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("owner-b", frozenset({"project-a"}))


def submission():
    return parse_submission(json.dumps({
        "project_id": "project-a", "title": "Private title",
        "prompt": "SECRET PROMPT NEVER LOGGED",
    }).encode())


class StateTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-58-")
        base = Path(self.temp.name)
        self.workspaces = base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        (self.workspaces / "slot-a").mkdir(mode=0o700)
        self.root = WorkspaceRoot(self.workspaces)
        self.path = base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def reopen(self):
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)

    def create(self, scope=OWNER):
        return self.inbox.accept(submission(), scope).task_id

    def claim(self, task_id):
        return self.inbox.claim_task(task_id, "worker-a", "slot-a", OWNER)

    def move(self, task_id, target, *, actor="worker-a", scope=OWNER):
        return self.inbox.transition_state(task_id, target, scope, actor_id=actor)

    def history(self, task_id):
        return self.inbox.transition_history(task_id, OWNER)

    def test_every_graph_edge_and_nonedge_has_a_boolean_answer(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(TaskState))
        for source in TaskState:
            for target in TaskState:
                with self.subTest(source=source, target=target):
                    self.assertIs(can_transition(source, target),
                                  target in ALLOWED_TRANSITIONS[source])

    def test_claim_running_succeeded_and_audit_survive_restart(self):
        task_id = self.create()
        dependent = self.create()
        self.inbox.set_dependencies(dependent, (task_id,), OWNER)
        self.claim(task_id)
        self.assertIsNone(self.inbox.select_next("project-a", OWNER))
        self.assertIs(self.move(task_id, TaskState.RUNNING), TaskState.RUNNING)
        self.assertIs(self.move(task_id, TaskState.SUCCEEDED), TaskState.SUCCEEDED)
        self.assertEqual(self.inbox.select_next("project-a", OWNER).task_id,
                         dependent)
        first = self.history(task_id)
        self.assertEqual([(e.from_state, e.to_state, e.accepted, e.reason_code)
                          for e in first], [
            (TaskState.QUEUED, TaskState.ASSIGNED, True, "claim"),
            (TaskState.ASSIGNED, TaskState.RUNNING, True, "transition"),
            (TaskState.RUNNING, TaskState.SUCCEEDED, True, "transition"),
        ])
        self.assertEqual([e.event_id for e in first], sorted(e.event_id for e in first))
        self.assertTrue(all(e.occurred_at for e in first))
        self.assertNotIn("SECRET PROMPT", str(first))
        self.assertNotIn(str(self.workspaces), str(first))
        self.reopen()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.SUCCEEDED)
        self.assertEqual(self.history(task_id), first)

    def test_queued_cancelled_skips_scheduler_and_claim_is_denied_and_audited(self):
        task_id = self.create()
        self.assertEqual(self.move(task_id, TaskState.CANCELLED, actor="operator"),
                         TaskState.CANCELLED)
        self.assertIsNone(self.inbox.select_next("project-a", OWNER))
        with self.assertRaises(TransitionRejected) as rejected:
            self.claim(task_id)
        self.assertEqual(rejected.exception.reason_code, "not_queued")
        self.assertEqual([(e.accepted, e.reason_code) for e in self.history(task_id)],
                         [(True, "transition"), (False, "not_queued")])
        self.reopen()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)
        self.assertIsNone(self.inbox.get_assignment(task_id, OWNER))
        self.assertIsNone(self.inbox.select_next("project-a", OWNER))

    def test_assigned_terminal_edges_are_possible(self):
        for target in (TaskState.FAILED, TaskState.CANCELLED,
                       TaskState.TIMED_OUT):
            with self.subTest(target=target):
                task_id = self.create()
                self.claim(task_id)
                self.move(task_id, target)
                self.assertIs(self.inbox.state(task_id, OWNER), target)
                self.assertEqual(len(self.history(task_id)), 2)

    def test_running_terminal_edges_are_possible(self):
        for target in (TaskState.SUCCEEDED, TaskState.FAILED,
                       TaskState.CANCELLED, TaskState.TIMED_OUT):
            with self.subTest(target=target):
                task_id = self.create()
                self.claim(task_id)
                self.move(task_id, TaskState.RUNNING)
                self.move(task_id, target)
                self.assertIs(self.inbox.state(task_id, OWNER), target)

    def test_rejected_edges_keep_state_and_durable_audit(self):
        task_id = self.create()
        for requested in (TaskState.QUEUED, TaskState.RUNNING,
                          TaskState.SUCCEEDED, TaskState.FAILED,
                          TaskState.TIMED_OUT):
            with self.subTest(requested=requested):
                with self.assertRaises(TransitionRejected) as rejection:
                    self.move(task_id, requested)
                self.assertEqual(rejection.exception.reason_code, "invalid_edge")
                self.assertIs(self.inbox.state(task_id, OWNER), TaskState.QUEUED)
        self.reopen()
        events = self.history(task_id)
        self.assertEqual(len(events), 5)
        self.assertTrue(all(not event.accepted for event in events))
        self.assertTrue(all(event.reason_code == "invalid_edge" for event in events))

    def test_assignment_edge_requires_atomically_claimed_pair(self):
        task_id = self.create()
        with self.assertRaises(TransitionRejected) as rejected:
            self.move(task_id, TaskState.ASSIGNED)
        self.assertEqual(rejected.exception.reason_code, "claim_required")
        self.assertIsNone(self.inbox.get_assignment(task_id, OWNER))
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.QUEUED)
        self.assertEqual(self.history(task_id)[0].reason_code, "claim_required")
        self.claim(task_id)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)

    def test_duplicate_or_terminal_transition_is_denied_and_logged(self):
        task_id = self.create()
        self.claim(task_id)
        with self.assertRaises(TransitionRejected):
            self.move(task_id, TaskState.ASSIGNED)
        self.move(task_id, TaskState.RUNNING)
        self.move(task_id, TaskState.SUCCEEDED)
        for target in TaskState:
            with self.subTest(target=target), self.assertRaises(TransitionRejected):
                self.move(task_id, target)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.SUCCEEDED)
        self.reopen()
        self.assertEqual(len(self.history(task_id)), 3 + 1 + len(TaskState))
        self.assertEqual(sum(not e.accepted for e in self.history(task_id)),
                         len(TaskState) + 1)

    def test_worker_mismatch_is_denied_with_audit(self):
        task_id = self.create()
        self.claim(task_id)
        with self.assertRaises(TransitionRejected) as rejected:
            self.move(task_id, TaskState.RUNNING, actor="different-worker")
        self.assertEqual(rejected.exception.reason_code, "worker_mismatch")
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)
        self.assertEqual(self.history(task_id)[-1].actor_id, "different-worker")
        self.move(task_id, TaskState.RUNNING)

    def test_unknown_and_foreign_tasks_cannot_read_or_pollute_audit(self):
        task_id = self.create(FOREIGN)
        with self.assertRaises(TaskNotFound):
            self.move(task_id, TaskState.CANCELLED)
        with self.assertRaises(TaskNotFound):
            self.inbox.transition_history(task_id, OWNER)
        with self.assertRaises(TaskNotFound):
            self.move("task_" + "0" * 32, TaskState.CANCELLED)
        self.assertEqual(self.inbox.transition_history(task_id, FOREIGN), ())

    def test_invalid_inputs_fail_before_audit(self):
        task_id = self.create()
        with self.assertRaises(TypeError):
            self.move(task_id, "succeeded")
        with self.assertRaises(TypeError):
            self.move(task_id, None)
        self.assertEqual(self.history(task_id), ())

    def test_concurrent_transitions_exactly_one_wins_other_attempts_logged(self):
        task_id = self.create()
        self.claim(task_id)

        def compete(_):
            try:
                return self.move(task_id, TaskState.RUNNING) is TaskState.RUNNING
            except TransitionRejected:
                return False

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(compete, range(32)))
        self.assertEqual(results.count(True), 1)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        events = self.history(task_id)
        self.assertEqual(len(events), 33)  # claim + 32 transition attempts
        self.assertEqual(sum(e.accepted for e in events), 2)
        self.reopen()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.RUNNING)
        self.assertEqual(len(self.history(task_id)), 33)

    def test_storage_failure_does_not_acknowledge_or_change_ram(self):
        task_id = self.create()
        self.claim(task_id)
        with patch.object(self.inbox._store, "write_transition",
                          side_effect=StorageUnavailable("PRIVATE PROMPT")):
            with self.assertRaises(StorageUnavailable):
                self.move(task_id, TaskState.RUNNING)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)
        self.assertEqual(len(self.history(task_id)), 1)
        self.reopen()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)

    def test_denial_audit_storage_failure_fails_closed(self):
        task_id = self.create()
        with patch.object(self.inbox._store, "record_rejected_transition",
                          side_effect=StorageUnavailable("PRIVATE PROMPT")):
            with self.assertRaises(StorageUnavailable):
                self.move(task_id, TaskState.SUCCEEDED)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.QUEUED)
        self.assertEqual(self.history(task_id), ())

    def test_audit_sql_trigger_rolls_back_successful_state_update(self):
        task_id = self.create()
        self.claim(task_id)
        db = self.inbox._store._db()
        db.execute("""CREATE TRIGGER block_audit BEFORE INSERT
            ON task_transition_audit BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        try:
            with self.assertRaises(StorageUnavailable):
                self.move(task_id, TaskState.RUNNING)
            self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)
        finally:
            db.execute("DROP TRIGGER block_audit")
        self.reopen()
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)
        self.assertEqual(len(self.history(task_id)), 1)

    def test_migrate_v2_with_mixed_claims_without_losing_tasks(self):
        claimed = self.create()
        unclaimed = self.create()
        self.claim(claimed)
        self.inbox.close()
        store = SQLiteTaskStore(self.path)
        db = store._db()
        with db:
            db.execute("DROP TABLE task_provider_audit")
            db.execute("DROP TABLE task_permission_audit")
            db.execute("DROP TABLE task_deliveries")
            db.execute("DROP TABLE task_admission_keys")
            db.execute("DROP TABLE task_transition_audit")
            db.execute("DROP TABLE task_states")
            db.execute("PRAGMA user_version=2")
        store.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.assertEqual(self.inbox._store._db().execute("PRAGMA user_version").fetchone(),
                             (12,))
        self.assertIs(self.inbox.state(claimed, OWNER), TaskState.ASSIGNED)
        self.assertIs(self.inbox.state(unclaimed, OWNER), TaskState.QUEUED)
        self.assertEqual(self.history(claimed), ())  # old v2 has no audit
        self.assertIsNotNone(self.inbox.get_assignment(claimed, OWNER))
        self.assertIsNotNone(self.inbox.get(unclaimed, OWNER))

    def test_corrupt_persisted_claim_state_invariant_fails_closed(self):
        task_id = self.create()
        self.claim(task_id)
        with self.inbox._store._db() as db:
            db.execute("UPDATE task_states SET state='queued' WHERE task_id=?",
                       (task_id,))
        self.inbox.close()
        with self.assertRaises(StorageUnavailable):
            TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)

    def test_durable_audit_is_scoped_per_task_and_contains_no_sensitive_text(self):
        first = self.create()
        second = self.create()
        self.move(first, TaskState.CANCELLED, actor="operator")
        self.move(second, TaskState.CANCELLED, actor="operator")
        self.assertEqual(len(self.history(first)), 1)
        self.assertEqual(len(self.history(second)), 1)
        self.assertNotEqual(self.history(first)[0].event_id,
                            self.history(second)[0].event_id)
        raw = str(self.history(first) + self.history(second))
        self.assertNotIn("SECRET PROMPT", raw)
        self.assertNotIn("Private title", raw)


class VolatileTransitionTests(unittest.TestCase):
    def test_ram_only_audit_is_available_but_not_durable(self):
        inbox = TaskInbox()
        task_id = inbox.accept(submission(), OWNER).task_id
        inbox.transition_state(task_id, TaskState.CANCELLED, OWNER,
                               actor_id="operator")
        with self.assertRaises(TransitionRejected):
            inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                   actor_id="operator")
        events = inbox.transition_history(task_id, OWNER)
        self.assertEqual([(e.accepted, e.reason_code) for e in events],
                         [(True, "transition"), (False, "invalid_edge")])
        self.assertEqual([e.event_id for e in events], [1, 2])


class TransitionHTTPBoundaryTests(unittest.TestCase):
    def test_http_state_is_read_only_even_with_body(self):
        inbox = TaskInbox()
        task_id = inbox.accept(submission(), OWNER).task_id
        server = ThreadingHTTPServer(("127.0.0.1", 0), task_handler(inbox, OWNER))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=4)
            try:
                connection.request("PUT", f"/v1/tasks/{task_id}/state",
                                   body=b'{"task_state":"succeeded"}',
                                   headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 405)
                self.assertEqual(json.loads(response.read()),
                                 {"status": "method_not_allowed"})
            finally:
                connection.close()
            self.assertIs(inbox.state(task_id, OWNER), TaskState.QUEUED)
            inbox.transition_state(task_id, TaskState.CANCELLED, OWNER,
                                   actor_id="operator")
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=4)
            try:
                connection.request("GET", f"/v1/tasks/{task_id}/state")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["task_state"],
                                 "cancelled")
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
