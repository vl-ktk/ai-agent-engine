"""Stage 5.7: executable lifecycle schema and honest observed states."""

import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from ai_agent_engine.task_api import (
    OwnerScope, TaskInbox, parse_submission, task_handler,
)
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_state import (
    ALLOWED_TRANSITIONS, TERMINAL_STATES, TaskState,
    allowed_next_states, can_transition,
)
from ai_agent_engine.task_storage import SQLiteTaskStore


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("owner-b", frozenset({"project-a"}))


def submission():
    return parse_submission(json.dumps({
        "project_id": "project-a", "title": "Task", "prompt": "PRIVATE PROMPT",
    }).encode())


class LifecycleSchemaTests(unittest.TestCase):
    def test_enum_values_are_unique_and_stable(self):
        self.assertEqual({state.value for state in TaskState}, {
            "queued", "assigned", "running", "succeeded", "failed",
            "cancelled", "timed_out",
        })

    def test_graph_covers_each_state_and_has_no_implicit_self_loops(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(TaskState))
        for state, targets in ALLOWED_TRANSITIONS.items():
            self.assertIsInstance(targets, frozenset)
            self.assertNotIn(state, targets)
            self.assertTrue(targets <= set(TaskState))

    def test_queue_assignment_execution_success_path(self):
        self.assertTrue(can_transition(TaskState.QUEUED, TaskState.ASSIGNED))
        self.assertTrue(can_transition(TaskState.ASSIGNED, TaskState.RUNNING))
        self.assertTrue(can_transition(TaskState.RUNNING, TaskState.SUCCEEDED))

    def test_terminal_states_do_not_have_outgoing_edges(self):
        self.assertEqual(TERMINAL_STATES, frozenset({
            TaskState.SUCCEEDED, TaskState.FAILED,
            TaskState.CANCELLED, TaskState.TIMED_OUT,
        }))
        for terminal in TERMINAL_STATES:
            self.assertFalse(allowed_next_states(terminal))

    def test_graph_is_immutable_and_rejects_untyped_inputs(self):
        with self.assertRaises(TypeError):
            ALLOWED_TRANSITIONS[TaskState.QUEUED] = frozenset()
        with self.assertRaises(AttributeError):
            allowed_next_states(TaskState.QUEUED).add(TaskState.RUNNING)
        for candidate in ("queued", None, 0):
            with self.subTest(candidate=candidate), self.assertRaises(TypeError):
                allowed_next_states(candidate)
        with self.assertRaises(TypeError):
            can_transition(TaskState.QUEUED, "assigned")


class ObservedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ae-57-state-")
        base = Path(self.directory.name)
        self.workspaces = base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        (self.workspaces / "slot-a").mkdir(mode=0o700)
        self.db = base / "inbox.sqlite3"
        self.root = WorkspaceRoot(self.workspaces)
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)

    def tearDown(self):
        self.inbox.close()
        self.directory.cleanup()

    def test_queue_claim_and_durable_recovery(self):
        record = self.inbox.accept(submission(), OWNER)
        self.assertIs(self.inbox.state(record.task_id, OWNER), TaskState.QUEUED)
        self.inbox.claim_task(record.task_id, "worker-a", "slot-a", OWNER)
        self.assertIs(self.inbox.state(record.task_id, OWNER), TaskState.ASSIGNED)
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.assertIs(self.inbox.state(record.task_id, OWNER), TaskState.ASSIGNED)

    def test_missing_foreign_and_invalid_ids_have_no_state(self):
        record = self.inbox.accept(submission(), OWNER)
        self.assertIsNone(self.inbox.state(record.task_id, FOREIGN))
        self.assertIsNone(self.inbox.state("task_" + "0" * 32, OWNER))
        self.assertIsNone(self.inbox.state("../../invalid", OWNER))

    def test_state_reads_do_not_change_scheduler_or_assign_task(self):
        record = self.inbox.accept(submission(), OWNER)
        self.assertIs(self.inbox.state(record.task_id, OWNER), TaskState.QUEUED)
        self.assertEqual(self.inbox.select_next("project-a", OWNER).task_id,
                         record.task_id)
        self.assertIsNone(self.inbox.get_assignment(record.task_id, OWNER))


class LifecycleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ae-57-http-")
        base = Path(self.directory.name)
        workspace = base / "workspaces"
        workspace.mkdir(mode=0o700)
        (workspace / "slot-a").mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(base / "tasks.sqlite3"),
                               workspace_root=WorkspaceRoot(workspace))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          task_handler(self.inbox, OWNER))
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.inbox.close()
        self.directory.cleanup()

    def request(self, method, path, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            body = json.dumps(data).encode() if data is not None else None
            connection.request(method, path, body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_reads_actual_queued_then_assigned_state(self):
        status, created = self.request("POST", "/v1/tasks", {
            "project_id": "project-a", "title": "Task", "prompt": "PRIVATE PROMPT",
        })
        self.assertEqual(status, 202)
        task_id = created["task_id"]
        path = f"/v1/tasks/{task_id}/state"
        self.assertEqual(self.request("GET", path), (200, {
            "status": "state_found", "task_id": task_id, "task_state": "queued",
        }))
        self.assertEqual(self.request("POST", f"/v1/tasks/{task_id}/claim", {
            "worker_id": "worker-a", "workspace_name": "slot-a",
        })[0], 201)
        self.assertEqual(self.request("GET", path), (200, {
            "status": "state_found", "task_id": task_id, "task_state": "assigned",
        }))
        self.assertEqual(self.request("POST", path, {"task_state": "succeeded"}),
                         (404, {"status": "not_found"}))

    def test_http_does_not_expose_unknown_or_foreign_tasks(self):
        self.assertEqual(self.request("GET", "/v1/tasks/task_" + "0" * 32 + "/state"),
                         (404, {"status": "not_found"}))
        other = self.inbox.accept(submission(), FOREIGN)
        self.assertEqual(self.request("GET", f"/v1/tasks/{other.task_id}/state"),
                         (404, {"status": "not_found"}))
