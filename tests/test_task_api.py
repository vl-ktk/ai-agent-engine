"""Stage 5.1: local HTTP admission, malformed input and memory-only behavior."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from threading import Thread
import unittest

from ai_agent_engine.task_api import (
    MAX_BODY_BYTES, InboxFull, InvalidJSON, InvalidTask, TASK_PATH,
    TaskInbox, TaskSubmission, parse_submission, task_handler,
)


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
        self.assertEqual(inbox.accept(submission), 1)
        self.assertEqual(inbox.snapshot(), (submission,))
        with self.assertRaises(InboxFull):
            inbox.accept(submission)
        for invalid in (0, True, -1, 4097):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                TaskInbox(max_pending=invalid)
        with self.assertRaises(TypeError):
            inbox.accept({"project_id": "x"})


class TaskHTTPTests(unittest.TestCase):
    def setUp(self):
        self.inbox = TaskInbox(max_pending=20)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), task_handler(self.inbox))
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def exchange(self, body, *, method="POST", path=TASK_PATH, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
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
        self.assertEqual(result, {"status": "accepted_in_memory", "receipt": 1})
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertNotIn("Create a concise report", json.dumps(result))
        self.assertEqual(len(self.inbox.snapshot()), 1)
        self.assertEqual(self.inbox.snapshot()[0].project_id, "project-a")
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
        self.assertEqual(len(self.inbox.snapshot()), 0)

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
        self.assertFalse(self.inbox.snapshot())

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
        self.assertFalse(self.inbox.snapshot())

    def test_queue_capacity_blocks_without_accepting(self):
        self.inbox = TaskInbox(max_pending=1)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), task_handler(self.inbox))
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.exchange(valid_request())[0], 202)
        self.assertEqual(self.exchange(valid_request())[0:2],
                         (503, {"status": "inbox_full"}))
        self.assertEqual(len(self.inbox.snapshot()), 1)

    def test_simultaneous_admissions_have_unique_receipts(self):
        def submit(index):
            status, result, _ = self.exchange(valid_request(title=f"Task {index}"))
            return status, result["receipt"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(16)))
        self.assertTrue(all(status == 202 for status, _ in results))
        self.assertEqual(sorted(receipt for _, receipt in results), list(range(1, 17)))
        self.assertEqual(len(self.inbox.snapshot()), 16)


if __name__ == "__main__":
    unittest.main()
