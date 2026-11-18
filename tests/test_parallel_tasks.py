"""Stage 5.10: independent offline tasks progress together without work lock."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Barrier, Event, Thread
import unittest

from ai_agent_engine.task_api import (
    OwnerScope, TaskInbox, parse_submission, task_handler,
)
from ai_agent_engine.task_assignment import WorkspaceBusy, WorkspaceRoot
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


OWNER = OwnerScope("owner-a", frozenset({"project-a", "project-b"}))
OTHER_OWNER = OwnerScope("owner-b", frozenset({"project-a"}))


def submission(project="project-a", priority=0):
    return parse_submission(json.dumps({
        "project_id": project, "title": "Simulated offline task",
        "prompt": "NEVER SENT TO A MODEL", "priority": priority,
    }).encode())


class ParallelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-510-")
        self.base = Path(self.temp.name)
        self.workspaces = self.base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        for index in range(16):
            (self.workspaces / f"slot-{index}").mkdir(mode=0o700)
        self.root = WorkspaceRoot(self.workspaces)
        self.db = self.base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def create(self, *, scope=OWNER, project="project-a", priority=0):
        return self.inbox.accept(submission(project, priority), scope).task_id

    def claim(self, task_id, index, *, scope=OWNER):
        self.inbox.claim_task(task_id, f"worker-{index}", f"slot-{index}", scope)

    def running(self, task_id, index, *, scope=OWNER):
        self.inbox.transition_state(task_id, TaskState.RUNNING, scope,
                                    actor_id=f"worker-{index}")

    def succeeded(self, task_id, index, *, scope=OWNER):
        self.inbox.transition_state(task_id, TaskState.SUCCEEDED, scope,
                                    actor_id=f"worker-{index}")

    def test_eight_independent_tasks_simultaneously_reach_running_barrier(self):
        ids = [self.create() for _ in range(8)]
        together = Barrier(9, timeout=8)  # eight simulated workers + test thread
        release = Event()

        def simulate(index):
            task_id = ids[index]
            self.claim(task_id, index)
            self.running(task_id, index)
            together.wait()  # must all be RUNNING before any finishes
            if not release.wait(timeout=8):
                raise AssertionError("simulated offline work was not released")
            self.succeeded(task_id, index)
            return task_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(simulate, index) for index in range(8)]
            try:
                together.wait()
                self.assertEqual([self.inbox.state(task_id, OWNER) for task_id in ids],
                                 [TaskState.RUNNING] * 8)
                for index in range(8):
                    with self.assertRaises(WorkspaceBusy):
                        self.root.acquire(f"slot-{index}")
            finally:
                release.set()
            self.assertEqual({future.result(timeout=8) for future in futures}, set(ids))
        self.assertEqual([self.inbox.state(task_id, OWNER) for task_id in ids],
                         [TaskState.SUCCEEDED] * 8)
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.assertEqual([self.inbox.state(task_id, OWNER) for task_id in ids],
                         [TaskState.SUCCEEDED] * 8)
        for task_id in ids:
            self.assertEqual([event.to_state for event in
                              self.inbox.transition_history(task_id, OWNER)],
                             [TaskState.ASSIGNED, TaskState.RUNNING,
                              TaskState.SUCCEEDED])

    def test_one_paused_simulated_worker_does_not_block_another_task(self):
        first, second = self.create(), self.create()
        working = Event()
        continue_work = Event()
        errors = []

        def paused_work():
            try:
                self.claim(first, 0)
                self.running(first, 0)
                working.set()
                if not continue_work.wait(timeout=8):
                    raise AssertionError("work did not resume")
                self.succeeded(first, 0)
            except Exception as exc:
                errors.append(exc)
                working.set()

        thread = Thread(target=paused_work)
        thread.start()
        try:
            self.assertTrue(working.wait(timeout=8))
            self.assertFalse(errors)
            self.assertEqual(self.inbox.select_next("project-a", OWNER).task_id,
                             second)
            self.claim(second, 1)
            self.running(second, 1)
            self.assertIs(self.inbox.state(first, OWNER), TaskState.RUNNING)
            self.assertIs(self.inbox.state(second, OWNER), TaskState.RUNNING)
            self.succeeded(second, 1)
        finally:
            continue_work.set()
            thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertIs(self.inbox.state(first, OWNER), TaskState.SUCCEEDED)

    def test_dependency_blocked_task_does_not_block_independent_sibling(self):
        prerequisite = self.create(priority=10)
        dependent = self.create(priority=100)
        independent = self.create(priority=80)
        self.inbox.set_dependencies(dependent, (prerequisite,), OWNER)
        self.assertEqual(self.inbox.select_next("project-a", OWNER).task_id,
                         independent)
        self.claim(independent, 0)
        self.running(independent, 0)
        self.assertEqual(self.inbox.select_next("project-a", OWNER).task_id,
                         prerequisite)
        self.claim(prerequisite, 1)
        self.running(prerequisite, 1)
        self.assertIsNone(self.inbox.select_next("project-a", OWNER))
        self.succeeded(prerequisite, 1)
        self.assertEqual(self.inbox.select_next("project-a", OWNER).task_id,
                         dependent)
        self.assertIs(self.inbox.state(independent, OWNER), TaskState.RUNNING)
        self.claim(dependent, 2)
        self.running(dependent, 2)
        self.assertIs(self.inbox.state(dependent, OWNER), TaskState.RUNNING)

    def test_same_worker_can_handle_two_distinct_workspaces(self):
        first, second = self.create(), self.create()
        self.inbox.claim_task(first, "worker-shared", "slot-0", OWNER)
        self.inbox.claim_task(second, "worker-shared", "slot-1", OWNER)
        for task_id in (first, second):
            self.inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                        actor_id="worker-shared")
        self.assertIs(self.inbox.state(first, OWNER), TaskState.RUNNING)
        self.assertIs(self.inbox.state(second, OWNER), TaskState.RUNNING)

    def test_different_owner_and_project_scopes_can_work_independently(self):
        first = self.create(project="project-a")
        second = self.create(project="project-b")
        third = self.create(scope=OTHER_OWNER)
        owners = [(first, 0, OWNER), (second, 1, OWNER),
                  (third, 2, OTHER_OWNER)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(lambda item: (self.claim(item[0], item[1],
                scope=item[2]), self.running(item[0], item[1], scope=item[2])), item)
                for item in owners]
            for future in futures:
                future.result(timeout=5)
        for task_id, _, scope in owners:
            self.assertIs(self.inbox.state(task_id, scope), TaskState.RUNNING)
        self.assertIsNone(self.inbox.state(third, OWNER))
        self.assertIsNone(self.inbox.state(first, OTHER_OWNER))

    def test_conflict_is_local_to_one_workspace_not_all_others(self):
        first, blocked, independent = self.create(), self.create(), self.create()
        self.claim(first, 0)
        self.running(first, 0)
        with self.assertRaises(WorkspaceBusy):
            self.claim(blocked, 0)
        self.claim(independent, 1)
        self.running(independent, 1)
        self.assertIs(self.inbox.state(blocked, OWNER), TaskState.QUEUED)
        self.assertIs(self.inbox.state(independent, OWNER), TaskState.RUNNING)
        self.assertEqual(self.inbox.transition_history(blocked, OWNER)[-1].reason_code,
                         "workspace_busy")

    def test_two_separate_inboxes_do_not_share_a_global_python_lock(self):
        other = TaskInbox(store=SQLiteTaskStore(self.base / "other.sqlite3"),
                          workspace_root=WorkspaceRoot(self.workspaces))
        try:
            first = self.create()
            second = other.accept(submission(), OTHER_OWNER).task_id
            together = Barrier(3, timeout=5)

            def simulate(inbox, task_id, worker, slot, scope):
                inbox.claim_task(task_id, worker, slot, scope)
                inbox.transition_state(task_id, TaskState.RUNNING,
                                       scope, actor_id=worker)
                together.wait()
                return inbox.state(task_id, scope)

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(simulate, self.inbox, first, "worker-a", "slot-0", OWNER)
                b = pool.submit(simulate, other, second, "worker-b", "slot-1",
                                OTHER_OWNER)
                together.wait()
                self.assertIs(a.result(timeout=5), TaskState.RUNNING)
                self.assertIs(b.result(timeout=5), TaskState.RUNNING)
        finally:
            other.close()


class ParallelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-510-http-")
        self.base = Path(self.temp.name)
        root_path = self.base / "workspaces"
        root_path.mkdir(mode=0o700)
        for index in range(12):
            (root_path / f"slot-{index}").mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.base / "tasks.sqlite3"),
                               workspace_root=WorkspaceRoot(root_path))
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
        self.temp.cleanup()

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_12_independent_http_claims_all_succeed_without_global_workspace_lock(self):
        ids = []
        for _ in range(12):
            status, body = self.request("POST", "/v1/tasks", {
                "project_id": "project-a", "title": "Parallel test",
                "prompt": "No model used",
            })
            self.assertEqual(status, 202)
            ids.append(body["task_id"])

        def claim(item):
            index, task_id = item
            return self.request("POST", f"/v1/tasks/{task_id}/claim", {
                "worker_id": f"worker-{index}",
                "workspace_name": f"slot-{index}",
            })

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(claim, enumerate(ids)))
        self.assertEqual(sum(status == 201 for status, _ in results), 12)
        self.assertEqual({body["workspace_name"] for _, body in results},
                         {f"slot-{index}" for index in range(12)})
        for index, task_id in enumerate(ids):
            self.inbox.transition_state(task_id, TaskState.RUNNING,
                                        OWNER, actor_id=f"worker-{index}")
            status, read = self.request("GET", f"/v1/tasks/{task_id}/state")
            self.assertEqual(status, 200)
            self.assertEqual(read["task_state"], "running")
