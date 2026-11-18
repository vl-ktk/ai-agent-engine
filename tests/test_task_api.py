"""Stages 5.1-5.2: local HTTP admission, stable IDs and owner isolation."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import http.client
from http.server import ThreadingHTTPServer
from io import StringIO
import json
from threading import Thread
import unittest
from uuid import UUID

from ai_agent_engine.task_api import (
    MAX_BODY_BYTES, InboxFull, InvalidJSON, InvalidTask, TASK_PATH,
    OwnerScope, ParentNotFound, ProjectForbidden, TaskInbox,
    TaskSubmission, main, parse_submission, task_handler,
)


OWNER_A = OwnerScope("owner-a", frozenset({"project-a", "project-b"}))
OWNER_B = OwnerScope("owner-b", frozenset({"project-a"}))
OWNER_A_RESTRICTED = OwnerScope("owner-a", frozenset({"project-b"}))


def valid_request(**overrides):
    payload = {
        "project_id": "project-a",
        "title": "Prepare report",
        "prompt": "Create a concise report for the owner.",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class SubmissionContractTests(unittest.TestCase):
    def test_valid_submission_is_immutable(self):
        result = parse_submission(valid_request(project_id="project-b", title="Новый проект"))
        self.assertEqual(result.project_id, "project-b")
        self.assertEqual(result.title, "Новый проект")
        with self.assertRaises(AttributeError):
            result.title = "modified"

    def test_parent_id_is_optional_and_canonical(self):
        task_id = "task_" + "a" * 32
        subtask_id = "subtask_" + "b" * 32
        for parent_id in (task_id, subtask_id):
            submission = parse_submission(valid_request(parent_task_id=parent_id))
            self.assertEqual(submission.parent_task_id, parent_id)
        self.assertIsNone(parse_submission(valid_request()).parent_task_id)
        for parent_id in (None, "", "1", "task_ABC", "task_" + "F" * 32,
                          "subtask_" + "z" * 32, "../task", 100):
            with self.subTest(parent_id=parent_id), self.assertRaises(InvalidTask):
                parse_submission(valid_request(parent_task_id=parent_id))

    def test_required_fields_are_exact(self):
        for excluded in ("project_id", "title", "prompt"):
            data = json.loads(valid_request())
            del data[excluded]
            with self.subTest(excluded=excluded), self.assertRaises(InvalidTask):
                parse_submission(json.dumps(data).encode())
        with self.assertRaises(InvalidTask):
            parse_submission(valid_request(owner_id="unapproved"))

    def test_invalid_values(self):
        for changes in (
            {"project_id": "../other-project"},
            {"project_id": ""},
            {"project_id": "x" * 129},
            {"title": " \t "},
            {"title": "t" * 1025},
            {"title": "bad\nname"},
            {"prompt": ""},
            {"prompt": "a" * 32769},
            {"prompt": "bad\x00text"},
            {"title": 100},
            {"prompt": ["not", "text"]},
            {"project_id": None},
        ):
            with self.subTest(changes=repr(changes)[:80]), self.assertRaises(InvalidTask):
                parse_submission(valid_request(**changes))

    def test_json_failures(self):
        for data in (
            b"", b"not-json", b"[]", b"{",
            b'{"project_id":"a","project_id":"b","title":"t","prompt":"p"}',
            b"\xff", b" " * (MAX_BODY_BYTES + 1),
        ):
            with self.subTest(sample=data[:24]):
                expected = InvalidTask if data == b"[]" else InvalidJSON
                with self.assertRaises(expected):
                    parse_submission(data)
        with self.assertRaises(InvalidTask):
            parse_submission(b'{"project_id":"a","title":"t","prompt":"\\ud800"}')

    def test_inbox_has_bounded_capacity_and_receipts(self):
        inbox = TaskInbox(max_pending=1)
        submission = parse_submission(valid_request())
        record = inbox.accept(submission, OWNER_A)
        self.assertEqual(record.receipt, 1)
        self.assertEqual(inbox.snapshot(OWNER_A), (record,))
        with self.assertRaises(InboxFull):
            inbox.accept(submission, OWNER_A)
        for invalid in (0, True, -1, 4097):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                TaskInbox(max_pending=invalid)
        with self.assertRaises(TypeError):
            inbox.accept({"project_id": "x"}, OWNER_A)


class TaskIdentityTests(unittest.TestCase):
    def setUp(self):
        self.inbox = TaskInbox()
        self.base = parse_submission(valid_request())

    def test_scopes_must_be_explicit_and_valid(self):
        for owner_id, projects in (("", frozenset({"project-a"})),
                                   ("../owner", frozenset({"project-a"})),
                                   ("owner-a", frozenset()),
                                   ("owner-a", frozenset({"../other"})),
                                   ("owner-a", {"project-a"})):
            with self.subTest(owner_id=owner_id, projects=projects), self.assertRaises(ValueError):
                OwnerScope(owner_id, projects)
        with self.assertRaises(TypeError):
            task_handler(self.inbox, None)
        with self.assertRaises(TypeError):
            self.inbox.accept(self.base, None)
        with self.assertRaises(TypeError):
            self.inbox.snapshot(None)

    def test_server_cli_rejects_missing_owner_scope(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as missing_owner:
                main(["--project-id", "project-a"])
            self.assertEqual(missing_owner.exception.code, 2)
            with self.assertRaises(SystemExit) as missing_project:
                main(["--owner-id", "owner-a"])
            self.assertEqual(missing_project.exception.code, 2)
            with self.assertRaises(SystemExit) as invalid_owner:
                main(["--owner-id", "../other", "--project-id", "project-a"])
            self.assertEqual(invalid_owner.exception.code, 2)

    def test_task_subtask_ids_unique_stable_and_linked(self):
        parent = self.inbox.accept(self.base, OWNER_A)
        child = self.inbox.accept(parse_submission(valid_request(
            parent_task_id=parent.task_id, title="child")), OWNER_A)
        grandchild = self.inbox.accept(parse_submission(valid_request(
            parent_task_id=child.task_id, title="grandchild")), OWNER_A)
        for record, prefix in ((parent, "task_"), (child, "subtask_"),
                               (grandchild, "subtask_")):
            self.assertTrue(record.task_id.startswith(prefix))
            self.assertEqual(UUID(hex=record.task_id[len(prefix):]).version, 4)
            self.assertIs(self.inbox.get(record.task_id, OWNER_A), record)
            self.assertEqual(record.owner_id, "owner-a")
        self.assertEqual(len({parent.task_id, child.task_id, grandchild.task_id}), 3)
        self.assertIsNone(parent.parent_task_id)
        self.assertEqual(child.parent_task_id, parent.task_id)
        self.assertEqual(grandchild.parent_task_id, child.task_id)
        with self.assertRaises(AttributeError):
            parent.task_id = "task_" + "0" * 32
        self.assertIsNone(TaskInbox().get(parent.task_id, OWNER_A))  # RAM-only, 5.5

    def test_foreign_owner_cannot_get_or_enumerate_shared_project(self):
        record = self.inbox.accept(self.base, OWNER_A)
        self.assertIsNone(self.inbox.get(record.task_id, OWNER_B))
        self.assertIsNone(self.inbox.get(record.task_id, OWNER_A_RESTRICTED))
        self.assertEqual(self.inbox.snapshot(OWNER_B), ())
        self.assertEqual(self.inbox.snapshot(OWNER_A_RESTRICTED), ())
        other = self.inbox.accept(self.base, OWNER_B)
        self.assertEqual(self.inbox.snapshot(OWNER_A), (record,))
        self.assertEqual(self.inbox.snapshot(OWNER_B), (other,))
        self.assertIsNone(self.inbox.get(other.task_id, OWNER_A))
        self.assertIsNone(self.inbox.get("task_" + "0" * 32, OWNER_A))

    def test_cross_owner_and_project_parent_rejected_without_admission(self):
        parent = self.inbox.accept(self.base, OWNER_A)
        foreign = parse_submission(valid_request(parent_task_id=parent.task_id))
        with self.assertRaises(ParentNotFound):
            self.inbox.accept(foreign, OWNER_B)
        with self.assertRaises(ParentNotFound):
            self.inbox.accept(parse_submission(valid_request(
                project_id="project-b", parent_task_id=parent.task_id)), OWNER_A)
        with self.assertRaises(ParentNotFound):
            self.inbox.accept(parse_submission(valid_request(
                parent_task_id="task_" + "f" * 32)), OWNER_A)
        self.assertEqual(len(self.inbox.snapshot(OWNER_A)), 1)
        self.assertFalse(self.inbox.snapshot(OWNER_B))

    def test_forbidden_project_not_admitted(self):
        with self.assertRaises(ProjectForbidden):
            self.inbox.accept(parse_submission(valid_request(project_id="project-c")),
                              OWNER_A)
        self.assertEqual(self.inbox.snapshot(OWNER_A), ())


class TaskHTTPTests(unittest.TestCase):
    def setUp(self):
        self.inbox = TaskInbox(max_pending=20)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          task_handler(self.inbox, OWNER_A))
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def exchange(self, body, *, method="POST", path=TASK_PATH, headers=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=4)
        try:
            conn.request(method, path, body=body, headers=(
                {"Content-Type": "application/json"} if headers is None else headers))
            response = conn.getresponse()
            payload = json.loads(response.read())
            return response.status, payload, response
        finally:
            conn.close()

    def raw_headers(self, headers):
        """Send exactly the supplied headers, without auto Content-Length."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.putrequest("POST", TASK_PATH)
            for key, value in headers:
                conn.putheader(key, value)
            conn.endheaders()
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_success_returns_202_and_keeps_submission_in_memory(self):
        status, result, response = self.exchange(valid_request())
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "accepted_in_memory")
        self.assertEqual(result["receipt"], 1)
        self.assertTrue(result["task_id"].startswith("task_"))
        self.assertEqual(result["owner_id"], "owner-a")
        self.assertEqual(result["project_id"], "project-a")
        self.assertIsNone(result["parent_task_id"])
        self.assertEqual(result["kind"], "task")
        self.assertEqual(result["priority"], 0)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertNotIn("Create a concise report", json.dumps(result))
        self.assertEqual(len(self.inbox.snapshot(OWNER_A)), 1)
        self.assertEqual(self.inbox.snapshot(OWNER_A)[0].project_id, "project-a")
        status, result, _ = self.exchange(
            valid_request(title="Second"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(result["receipt"], 2)

    def test_missing_invalid_and_unknown_fields_are_rejected(self):
        for body in (
            b'{"title":"t","prompt":"p"}',
            valid_request(prompt=" "),
            valid_request(project_id="other/../../project"),
            valid_request(model="paid-provider"),
        ):
            with self.subTest(body=body[:50]):
                self.assertEqual(self.exchange(body)[0:2], (422, {"status": "invalid_task"}))
        self.assertEqual(len(self.inbox.snapshot(OWNER_A)), 0)

    def test_malformed_duplicate_and_invalid_utf8_are_rejected(self):
        for body in (
            b'{', b"broken-secret=TOP_SECRET",
            b'{"project_id":"a","project_id":"b","title":"t","prompt":"p"}',
            b"\xff",
        ):
            with self.subTest(body=body[:40]):
                status, result, _ = self.exchange(body)
                self.assertEqual((status, result), (400, {"status": "invalid_json"}))
                self.assertNotIn("TOP_SECRET", json.dumps(result))
        self.assertFalse(self.inbox.snapshot(OWNER_A))

    def test_media_type_length_limits_and_methods(self):
        self.assertEqual(
            self.exchange(valid_request(), headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.exchange(b"", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.exchange(b"x" * (MAX_BODY_BYTES + 1))[0], 413)
        self.assertEqual(self.raw_headers([("Content-Type", "application/json")])[0], 411)
        self.assertEqual(self.raw_headers([("Content-Length", "-2")])[0], 400)
        self.assertEqual(self.raw_headers([("Content-Length", "9" * 5000)])[0], 413)
        self.assertEqual(self.raw_headers([("Content-Length", "2"),
                                          ("Content-Length", "2")])[0], 400)
        self.assertEqual(self.raw_headers([("Transfer-Encoding", "chunked")])[0], 400)
        self.assertEqual(self.exchange(b"", method="GET")[0], 405)
        self.assertEqual(self.exchange(valid_request(), path="/v1/other")[0], 404)
        self.assertFalse(self.inbox.snapshot(OWNER_A))

    def test_queue_capacity_blocks_without_accepting(self):
        self.inbox = TaskInbox(max_pending=1)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          task_handler(self.inbox, OWNER_A))
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.exchange(valid_request())[0], 202)
        self.assertEqual(self.exchange(valid_request())[0:2],
                         (503, {"status": "inbox_full"}))
        self.assertEqual(len(self.inbox.snapshot(OWNER_A)), 1)

    def test_simultaneous_admissions_have_unique_receipts(self):
        def submit(index):
            status, result, _ = self.exchange(valid_request(title=f"Task {index}"))
            return status, result["receipt"], result["task_id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(16)))
        self.assertTrue(all(status == 202 for status, _, _ in results))
        self.assertEqual(sorted(receipt for _, receipt, _ in results), list(range(1, 17)))
        self.assertEqual(len({task_id for _, _, task_id in results}), 16)
        self.assertEqual(len(self.inbox.snapshot(OWNER_A)), 16)

    def test_parent_child_http_read_and_no_prompt_echo(self):
        status, parent, _ = self.exchange(valid_request(prompt="PRIVATE-SECRET"))
        self.assertEqual(status, 202)
        status, child, _ = self.exchange(valid_request(
            parent_task_id=parent["task_id"], title="Subtask", prompt="SUBTASK-SECRET"))
        self.assertEqual(status, 202)
        self.assertTrue(child["task_id"].startswith("subtask_"))
        self.assertEqual(child["parent_task_id"], parent["task_id"])
        self.assertEqual(child["kind"], "subtask")
        status, metadata, response = self.exchange(
            b"", method="GET", path=f"{TASK_PATH}/{child['task_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(metadata, {"status": "found", **{
            key: child[key] for key in ("task_id", "owner_id", "project_id",
                                    "parent_task_id", "kind")},
                                    "depends_on": [], "priority": 0})
        self.assertNotIn("SECRET", json.dumps(metadata))
        self.assertNotIn("Subtask", json.dumps(metadata))
        self.assertEqual(self.exchange(b"", method="GET",
            path=f"{TASK_PATH}/{child['task_id']}")[1], metadata)

    def test_http_owner_and_project_isolation_even_with_known_id(self):
        _, parent, _ = self.exchange(valid_request())
        foreign_server = ThreadingHTTPServer(("127.0.0.1", 0),
            task_handler(self.inbox, OWNER_B))
        foreign_thread = Thread(target=foreign_server.serve_forever, daemon=True)
        foreign_thread.start()
        try:
            port = foreign_server.server_address[1]
            for task_id in (parent["task_id"], "task_" + "0" * 32):
                status, result, _ = self.exchange(b"", method="GET",
                    path=f"{TASK_PATH}/{task_id}", port=port)
                self.assertEqual((status, result), (404, {"status": "not_found"}))
            status, result, _ = self.exchange(valid_request(
                parent_task_id=parent["task_id"]), port=port)
            self.assertEqual((status, result), (404, {"status": "parent_not_found"}))
            status, owner_b_record, _ = self.exchange(valid_request(), port=port)
            self.assertEqual(status, 202)
            self.assertEqual(owner_b_record["owner_id"], "owner-b")
            status, result, _ = self.exchange(b"", method="GET",
                path=f"{TASK_PATH}/{owner_b_record['task_id']}")
            self.assertEqual((status, result), (404, {"status": "not_found"}))
        finally:
            foreign_server.shutdown()
            foreign_server.server_close()
            foreign_thread.join(timeout=3)

    def test_http_forbidden_projects_and_spoofing_are_rejected(self):
        self.assertEqual(self.exchange(valid_request(project_id="project-c"))[0:2],
                         (403, {"status": "project_forbidden"}))
        self.assertEqual(self.exchange(valid_request(owner_id="owner-b"))[0:2],
                         (422, {"status": "invalid_task"}))
        self.assertEqual(self.exchange(valid_request(parent_task_id="bad"))[0:2],
                         (422, {"status": "invalid_task"}))
        self.assertEqual(self.exchange(b"", method="GET",
            path="/v1/tasks/task_" + "f" * 32)[0:2],
            (404, {"status": "not_found"}))
        self.assertEqual(self.exchange(b"", method="GET", path="/v1/tasks/invalid")[0:2],
                         (404, {"status": "not_found"}))
        self.assertFalse(self.inbox.snapshot(OWNER_A))


if __name__ == "__main__":
    unittest.main()
