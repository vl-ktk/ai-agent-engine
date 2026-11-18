"""Stage 5.3: deterministic dependency ordering, cycles and HTTP isolation."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from threading import Thread
import unittest

from ai_agent_engine.task_dag import DependencyCycle, TaskDAG
from ai_agent_engine.task_api import (
    DependencyNotFound, InvalidJSON, InvalidTask, OwnerScope,
    ProjectForbidden, TaskInbox, TaskNotFound, parse_dependency_update,
    parse_submission, task_handler,
)


OWNER_A = OwnerScope("owner-a", frozenset({"project-a", "project-b"}))
OWNER_B = OwnerScope("owner-b", frozenset({"project-a"}))


def submission(project_id="project-a", **kwargs):
    return parse_submission(json.dumps({
        "project_id": project_id, "title": "task", "prompt": "task body", **kwargs,
    }).encode())


class DAGContractTests(unittest.TestCase):
    def test_json_contract_and_duplicate_keys(self):
        task_id = "task_" + "a" * 32
        self.assertEqual(parse_dependency_update(json.dumps({
            "depends_on": [task_id],
        }).encode()), (task_id,))
        self.assertEqual(parse_dependency_update(b'{"depends_on":[]}'), ())
        for invalid in (
            b"[]", b"{}", b'{"depends_on":null}', b'{"depends_on":"task"}',
            b'{"depends_on":[1]}', b'{"depends_on":["invalid"]}',
            json.dumps({"depends_on": [task_id, task_id]}).encode(),
            json.dumps({"depends_on": [task_id] * 65}).encode(),
            b'{"depends_on":[],"owner_id":"owner-b"}',
        ):
            with self.subTest(invalid=invalid[:50]), self.assertRaises(InvalidTask):
                parse_dependency_update(invalid)
        for invalid in (
            b"{", b"\xff", b'{"depends_on":[],"depends_on":[]}',
        ):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidJSON):
                parse_dependency_update(invalid)

    def test_linear_order(self):
        graph = TaskDAG()
        for index, task in enumerate("abcd", 1):
            graph.add_task(task, index)
        graph.set_dependencies("d", ("c",))
        graph.set_dependencies("c", ("b",))
        graph.set_dependencies("b", ("a",))
        self.assertEqual(graph.topological_order(("a", "b", "c", "d")),
                         ("a", "b", "c", "d"))

    def test_diamond_order_with_deterministic_ties(self):
        graph = TaskDAG()
        for index, task in enumerate("abcd", 1):
            graph.add_task(task, index)
        graph.set_dependencies("b", ("a",))
        graph.set_dependencies("c", ("a",))
        graph.set_dependencies("d", ("b", "c"))
        self.assertEqual(graph.topological_order(("d", "c", "a", "b")),
                         ("a", "b", "c", "d"))
        self.assertEqual(graph.dependencies("d"), ("b", "c"))

    def test_reversed_admission_order_places_prerequisites_first(self):
        graph = TaskDAG()
        graph.add_task("a", 1)
        graph.add_task("b", 2)
        graph.add_task("c", 3)
        graph.set_dependencies("a", ("c",))
        self.assertEqual(graph.topological_order(("a", "b", "c")),
                         ("b", "c", "a"))

    def test_self_and_long_cycle_rejected_atomically(self):
        graph = TaskDAG()
        for index, task in enumerate("abcd", 1):
            graph.add_task(task, index)
        graph.set_dependencies("b", ("a",))
        graph.set_dependencies("c", ("b",))
        graph.set_dependencies("d", ("c",))
        with self.assertRaises(DependencyCycle):
            graph.set_dependencies("a", ("a",))
        with self.assertRaises(DependencyCycle):
            graph.set_dependencies("a", ("d",))
        self.assertEqual(graph.dependencies("a"), ())
        self.assertEqual(graph.topological_order(tuple("abcd")), tuple("abcd"))

    def test_replacement_and_clearing_edges(self):
        graph = TaskDAG()
        for index, task in enumerate("abc", 1):
            graph.add_task(task, index)
        graph.set_dependencies("a", ("c",))
        graph.set_dependencies("a", ("b",))
        self.assertEqual(graph.dependencies("a"), ("b",))
        graph.set_dependencies("a", ())
        self.assertEqual(graph.topological_order(tuple("abc")), tuple("abc"))

    def test_missing_and_outside_subset_fail_closed(self):
        graph = TaskDAG()
        graph.add_task("a", 1)
        graph.add_task("b", 2)
        with self.assertRaises(KeyError):
            graph.set_dependencies("missing", ("a",))
        with self.assertRaises(KeyError):
            graph.set_dependencies("b", ("missing",))
        with self.assertRaises(ValueError):
            graph.set_dependencies("b", ("a", "a"))
        graph.set_dependencies("b", ("a",))
        for invalid in (("b",), ("b", "b"), ("unknown",)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                graph.topological_order(invalid)


class TaskDAGScopeTests(unittest.TestCase):
    def setUp(self):
        self.inbox = TaskInbox()
        self.first = self.inbox.accept(submission(), OWNER_A)
        self.second = self.inbox.accept(submission(), OWNER_A)

    def test_dependency_set_and_order(self):
        self.inbox.set_dependencies(self.first.task_id, (self.second.task_id,), OWNER_A)
        self.assertEqual(self.inbox.topological_order("project-a", OWNER_A),
                         (self.second.task_id, self.first.task_id))
        self.assertEqual(self.inbox.dependencies(self.first.task_id, OWNER_A),
                         (self.second.task_id,))

    def test_cross_owner_and_project_edges_forbidden(self):
        foreign_owner = self.inbox.accept(submission(), OWNER_B)
        foreign_project = self.inbox.accept(submission("project-b"), OWNER_A)
        for foreign_id in (foreign_owner.task_id, foreign_project.task_id):
            with self.subTest(foreign_id=foreign_id), self.assertRaises(DependencyNotFound):
                self.inbox.set_dependencies(self.first.task_id, (foreign_id,), OWNER_A)
        with self.assertRaises(TaskNotFound):
            self.inbox.set_dependencies(self.first.task_id, (), OWNER_B)
        with self.assertRaises(TaskNotFound):
            self.inbox.dependencies(self.first.task_id, OWNER_B)
        with self.assertRaises(ProjectForbidden):
            self.inbox.topological_order("project-b", OWNER_B)
        self.assertEqual(self.inbox.topological_order("project-a", OWNER_B),
                         (foreign_owner.task_id,))
        self.assertEqual(self.inbox.dependencies(self.first.task_id, OWNER_A), ())

    def test_parent_is_not_implicit_dependency(self):
        child = self.inbox.accept(submission(parent_task_id=self.first.task_id), OWNER_A)
        self.assertEqual(self.inbox.dependencies(child.task_id, OWNER_A), ())
        self.inbox.set_dependencies(child.task_id, (self.second.task_id,), OWNER_A)
        self.assertEqual(self.inbox.dependencies(child.task_id, OWNER_A),
                         (self.second.task_id,))

    def test_rejection_does_not_commit_partial_dependency_list(self):
        self.inbox.set_dependencies(self.first.task_id, (self.second.task_id,), OWNER_A)
        with self.assertRaises(DependencyNotFound):
            self.inbox.set_dependencies(self.first.task_id,
                (self.second.task_id, "task_" + "0" * 32), OWNER_A)
        self.assertEqual(self.inbox.dependencies(self.first.task_id, OWNER_A),
                         (self.second.task_id,))


class TaskDAGHTTPTests(unittest.TestCase):
    def setUp(self):
        self.inbox = TaskInbox(max_pending=32)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         task_handler(self.inbox, OWNER_A))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, *, method="GET", payload=None, port=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=5)
        try:
            body = None if payload is None else json.dumps(payload).encode()
            conn.request(method, path, body=body,
                headers={"Content-Type": "application/json"} if headers is None else headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def create(self, project_id="project-a", port=None):
        status, record = self.request("/v1/tasks", method="POST", port=port,
            payload={"project_id": project_id, "title": "Task", "prompt": "private"})
        self.assertEqual(status, 202)
        return record["task_id"]

    def update(self, task_id, dependencies, port=None):
        return self.request(f"/v1/tasks/{task_id}/dependencies", method="PUT",
                            payload={"depends_on": dependencies}, port=port)

    def test_live_http_order_and_cycle_rejection(self):
        a, b, c = (self.create() for _ in range(3))
        self.assertEqual(self.update(a, [c])[0], 200)
        self.assertEqual(self.update(c, [b])[0], 200)
        status, response = self.request("/v1/projects/project-a/task-order")
        self.assertEqual((status, response["task_ids"]), (200, [b, c, a]))
        self.assertEqual(self.update(b, [a]), (409, {"status": "dependency_cycle"}))
        status, response = self.request(f"/v1/tasks/{a}")
        self.assertEqual((status, response["depends_on"]), (200, [c]))
        self.assertNotIn("private", json.dumps(response))
        self.assertEqual(self.request("/v1/projects/project-a/task-order")[1]["task_ids"],
                         [b, c, a])

    def test_live_http_forbidden_cross_scope_and_unknown_dependency(self):
        a = self.create()
        b = self.create("project-b")
        self.assertEqual(self.update(a, [b]),
                         (404, {"status": "dependency_not_found"}))
        self.assertEqual(self.update(a, ["task_" + "0" * 32]),
                         (404, {"status": "dependency_not_found"}))
        self.assertEqual(self.request("/v1/projects/project-b/task-order")[1]["task_ids"],
                         [b])
        self.assertEqual(self.request("/v1/projects/project-c/task-order"),
                         (403, {"status": "project_forbidden"}))
        foreign = ThreadingHTTPServer(("127.0.0.1", 0),
                                     task_handler(self.inbox, OWNER_B))
        thread = Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        try:
            port = foreign.server_address[1]
            self.assertEqual(self.update(a, [], port=port),
                             (404, {"status": "not_found"}))
            another = self.create(port=port)
            self.assertEqual(self.update(a, [another]),
                             (404, {"status": "dependency_not_found"}))
            self.assertEqual(self.request("/v1/projects/project-a/task-order",
                                          port=port)[1]["task_ids"], [another])
        finally:
            foreign.shutdown()
            foreign.server_close()
            thread.join(timeout=3)

    def test_live_http_invalid_updates_and_atomic_replacement(self):
        a, b = self.create(), self.create()
        self.assertEqual(self.update(a, [b])[0], 200)
        for payload, expected in (({}, 422), ({"depends_on": "bad"}, 422),
                                  ({"depends_on": [b, b]}, 422),
                                  ({"depends_on": [b], "owner_id": "owner-b"}, 422)):
            with self.subTest(payload=payload):
                self.assertEqual(self.request(f"/v1/tasks/{a}/dependencies",
                    method="PUT", payload=payload)[0], expected)
        self.assertEqual(self.request(f"/v1/tasks/{a}")[1]["depends_on"], [b])
        self.assertEqual(self.update(a, []),
                         (200, {"status": "dependencies_updated", "task_id": a,
                                "depends_on": []}))
        self.assertEqual(self.request(f"/v1/tasks/{a}")[1]["depends_on"], [])
        self.assertEqual(self.request("/v1/projects/a/b/task-order"),
                         (404, {"status": "not_found"}))

    def test_concurrent_reverse_edges_cannot_both_succeed(self):
        a, b = self.create(), self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: self.update(*pair),
                                    [(a, [b]), (b, [a])]))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        order = self.request("/v1/projects/project-a/task-order")[1]["task_ids"]
        self.assertIn(order, ([a, b], [b, a]))


if __name__ == "__main__":
    unittest.main()
