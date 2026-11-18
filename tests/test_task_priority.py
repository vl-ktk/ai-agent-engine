"""Stage 5.4: deterministic priority selection with prerequisite barriers."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from threading import Thread
import unittest

from ai_agent_engine.task_api import (
    InvalidJSON, InvalidTask, OwnerScope, ProjectForbidden, TaskChoice,
    TaskInbox, TaskNotFound, TaskSubmission, parse_priority_update,
    parse_submission, task_handler,
)


OWNER_A = OwnerScope("owner-a", frozenset({"project-a", "project-b"}))
OWNER_B = OwnerScope("owner-b", frozenset({"project-a"}))


def task(priority=0, project_id="project-a"):
    data = {"project_id": project_id, "title": "Example", "prompt": "Not executed"}
    if priority is not None:
        data["priority"] = priority
    return parse_submission(json.dumps(data).encode())


class PriorityContractTests(unittest.TestCase):
    def test_submission_priority_defaults_to_zero_and_is_bounded(self):
        self.assertEqual(task(None).priority, 0)
        for priority in (0, 1, 50, 99, 100):
            with self.subTest(priority=priority):
                self.assertEqual(task(priority).priority, priority)
        for priority in (-1, 101, True, False, 1.0, "99", None):
            if priority is None:
                continue  # Omitted field means the documented default of zero.
            with self.subTest(priority=priority), self.assertRaises(InvalidTask):
                task(priority)

    def test_update_priority_requires_exact_json_integer(self):
        for priority in (0, 50, 100):
            self.assertEqual(parse_priority_update(json.dumps({
                "priority": priority,
            }).encode()), priority)
        invalid = (
            b"{}", b"[]", b'{"priority":null}', b'{"priority":true}',
            b'{"priority":1.5}', b'{"priority":-1}', b'{"priority":101}',
            b'{"priority":"100"}', b'{"priority":10,"owner_id":"owner-b"}',
        )
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(InvalidTask):
                parse_priority_update(body)

    def test_update_priority_rejects_malformed_json_and_duplicate_keys(self):
        for body in (b"{", b"\xff", b'{"priority":1,"priority":100}'):
            with self.subTest(body=body), self.assertRaises(InvalidJSON):
                parse_priority_update(body)


class PrioritySelectionTests(unittest.TestCase):
    def setUp(self):
        self.inbox = TaskInbox()

    def accept(self, priority=0, project_id="project-a", scope=OWNER_A):
        return self.inbox.accept(task(priority, project_id), scope)

    def test_highest_priority_wins_regardless_of_admission_order(self):
        low = self.accept(1)
        high = self.accept(100)
        medium = self.accept(50)
        choice = self.inbox.select_next("project-a", OWNER_A)
        self.assertIsInstance(choice, TaskChoice)
        self.assertEqual((choice.task_id, choice.priority, choice.receipt),
                         (high.task_id, 100, high.receipt))
        self.assertNotEqual(choice.task_id, low.task_id)
        self.assertNotEqual(choice.task_id, medium.task_id)
        with self.assertRaises(AttributeError):
            choice.priority = 0

    def test_equal_priority_ties_by_earliest_receipt(self):
        first = self.accept(50)
        second = self.accept(50)
        third = self.accept(50)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         first.task_id)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A,
                             completed_task_ids=(first.task_id,)).task_id,
                         second.task_id)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A,
                             completed_task_ids=(first.task_id, second.task_id)).task_id,
                         third.task_id)

    def test_dependencies_block_even_maximum_priority(self):
        prerequisite = self.accept(1)
        dependent = self.accept(100)
        unrelated = self.accept(50)
        self.inbox.set_dependencies(dependent.task_id, (prerequisite.task_id,), OWNER_A)
        choice = self.inbox.select_next("project-a", OWNER_A)
        self.assertEqual(choice.task_id, unrelated.task_id)
        choice = self.inbox.select_next("project-a", OWNER_A,
                         completed_task_ids=(prerequisite.task_id,))
        self.assertEqual(choice.task_id, dependent.task_id)
        self.assertEqual(choice.priority, 100)

    def test_reprioritize_changes_choice_without_changing_task_ids(self):
        first = self.accept(10)
        second = self.accept(5)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         first.task_id)
        self.assertEqual(self.inbox.set_priority(second.task_id, 99, OWNER_A), 99)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         second.task_id)
        self.assertEqual(self.inbox.priority(second.task_id, OWNER_A), 99)
        self.assertIs(self.inbox.get(second.task_id, OWNER_A), second)
        self.assertEqual(self.inbox.set_priority(second.task_id, 0, OWNER_A), 0)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         first.task_id)

    def test_all_completed_and_empty_project_have_no_next(self):
        self.assertIsNone(self.inbox.select_next("project-a", OWNER_A))
        item = self.accept(100)
        self.assertIsNone(self.inbox.select_next("project-a", OWNER_A,
                                                completed_task_ids=(item.task_id,)))
        self.assertIsNone(self.inbox.select_next("project-b", OWNER_A))

    def test_owner_project_isolation_and_untrusted_completion_rejection(self):
        item = self.accept(60)
        other_owner = self.accept(100, scope=OWNER_B)
        other_project = self.accept(99, project_id="project-b")
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         item.task_id)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_B).task_id,
                         other_owner.task_id)
        self.assertEqual(self.inbox.select_next("project-b", OWNER_A).task_id,
                         other_project.task_id)
        with self.assertRaises(ProjectForbidden):
            self.inbox.select_next("project-b", OWNER_B)
        for wrong in (other_owner.task_id, other_project.task_id,
                      "task_" + "f" * 32):
            with self.subTest(wrong=wrong), self.assertRaises(TaskNotFound):
                self.inbox.select_next("project-a", OWNER_A,
                                       completed_task_ids=(wrong,))
        with self.assertRaises(TaskNotFound):
            self.inbox.set_priority(item.task_id, 100, OWNER_B)
        with self.assertRaises(TaskNotFound):
            self.inbox.priority(item.task_id, OWNER_B)

    def test_invalid_priority_and_completed_contracts_leave_state_unchanged(self):
        item = self.accept(42)
        for priority in (True, -1, 101, 1.5, "30"):
            with self.subTest(priority=priority), self.assertRaises(InvalidTask):
                self.inbox.set_priority(item.task_id, priority, OWNER_A)
        with self.assertRaises(InvalidTask):
            self.inbox.accept(TaskSubmission("project-a", "test", "prompt",
                                            priority=True), OWNER_A)
        for completed in ([item.task_id], (item.task_id, item.task_id),
                          ("invalid",), (True,)):
            with self.subTest(completed=completed), self.assertRaises(InvalidTask):
                self.inbox.select_next("project-a", OWNER_A,
                                       completed_task_ids=completed)
        self.assertEqual(self.inbox.priority(item.task_id, OWNER_A), 42)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         item.task_id)

    def test_selection_is_observation_not_reservation(self):
        first = self.accept(20)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         first.task_id)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         first.task_id)
        self.assertEqual(len(self.inbox.snapshot(OWNER_A)), 1)


class PriorityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.inbox = TaskInbox(max_pending=32)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          task_handler(self.inbox, OWNER_A))
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=4)
        try:
            encoded = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=encoded,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def create(self, priority=None, port=None):
        payload = {"project_id": "project-a", "title": "test", "prompt": "secret"}
        if priority is not None:
            payload["priority"] = priority
        status, body = self.request("POST", "/v1/tasks", payload, port=port)
        self.assertEqual(status, 202)
        return body["task_id"], body["priority"]

    def test_http_create_update_read_and_next(self):
        low, init_low = self.create()
        high, init_high = self.create(100)
        self.assertEqual((init_low, init_high), (0, 100))
        self.assertEqual(self.request("GET", "/v1/projects/project-a/next-task"),
                         (200, {"status": "selected", "project_id": "project-a",
                                "task_id": high, "priority": 100}))
        self.assertEqual(self.request("PUT", f"/v1/tasks/{low}/priority",
                                      {"priority": 100}),
                         (200, {"status": "priority_updated", "task_id": low,
                                "priority": 100}))
        self.assertEqual(self.request("GET", "/v1/projects/project-a/next-task")[1]["task_id"],
                         low)  # equal priorities, earlier receipt wins
        status, metadata = self.request("GET", f"/v1/tasks/{low}")
        self.assertEqual((status, metadata["priority"]), (200, 100))
        self.assertNotIn("secret", json.dumps(metadata))

    def test_http_dependency_barrier_and_empty_selection(self):
        prerequisite, _ = self.create(0)
        dependent, _ = self.create(100)
        self.assertEqual(self.request("PUT", f"/v1/tasks/{dependent}/dependencies",
                                      {"depends_on": [prerequisite]})[0], 200)
        self.assertEqual(self.request("GET", "/v1/projects/project-a/next-task")[1]["task_id"],
                         prerequisite)
        self.assertEqual(self.request("GET", "/v1/projects/project-b/next-task"),
                         (200, {"status": "no_ready_task", "project_id": "project-b",
                                "task_id": None}))

    def test_http_invalid_priority_and_missing_tasks(self):
        good, _ = self.create(15)
        for priority in (True, "99", -1, 101, 1.1):
            with self.subTest(priority=priority):
                self.assertEqual(self.request("PUT", f"/v1/tasks/{good}/priority",
                                              {"priority": priority}),
                                 (422, {"status": "invalid_priority"}))
        self.assertEqual(self.request("PUT", f"/v1/tasks/{good}/priority",
                                      {"priority": 1, "owner_id": "owner-b"}),
                         (422, {"status": "invalid_priority"}))
        self.assertEqual(self.request("POST", "/v1/tasks", {
            "project_id": "project-a", "title": "test", "prompt": "test",
            "priority": True,
        }), (422, {"status": "invalid_task"}))
        self.assertEqual(self.request("PUT", "/v1/tasks/task_" + "0" * 32 + "/priority",
                                      {"priority": 10}),
                         (404, {"status": "not_found"}))
        self.assertEqual(self.request("GET", "/v1/projects/project-c/next-task"),
                         (403, {"status": "project_forbidden"}))
        self.assertEqual(self.request("GET", f"/v1/tasks/{good}")[1]["priority"], 15)

    def test_http_cross_owner_priority_mutation_rejected(self):
        task_id, _ = self.create(10)
        server_b = ThreadingHTTPServer(("127.0.0.1", 0),
                                       task_handler(self.inbox, OWNER_B))
        thread_b = Thread(target=server_b.serve_forever, daemon=True)
        thread_b.start()
        try:
            port_b = server_b.server_address[1]
            self.assertEqual(self.request("PUT", f"/v1/tasks/{task_id}/priority",
                                          {"priority": 100}, port=port_b),
                             (404, {"status": "not_found"}))
            self.assertEqual(self.request("GET", "/v1/projects/project-a/next-task",
                                          port=port_b),
                             (200, {"status": "no_ready_task", "project_id": "project-a",
                                    "task_id": None}))
            self.assertEqual(self.request("GET", f"/v1/tasks/{task_id}")[1]["priority"],
                             10)
        finally:
            server_b.shutdown()
            server_b.server_close()
            thread_b.join(timeout=3)

    def test_concurrent_priority_updates_remain_valid(self):
        task_id, _ = self.create(0)
        def update(priority):
            return self.request("PUT", f"/v1/tasks/{task_id}/priority",
                                {"priority": priority})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(update, range(20)))
        self.assertTrue(all(status == 200 for status, _ in results))
        choice = self.inbox.select_next("project-a", OWNER_A)
        self.assertEqual(choice.task_id, task_id)
        self.assertIn(choice.priority, range(20))
        self.assertEqual(self.inbox.priority(task_id, OWNER_A), choice.priority)


if __name__ == "__main__":
    unittest.main()
