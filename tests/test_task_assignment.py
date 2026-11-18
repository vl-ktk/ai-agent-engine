"""Stage 5.6: at-most-once worker+workspace binding under contention."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import (
    InvalidJSON, OwnerScope, TaskInbox, TaskNotFound, parse_assignment_request,
    parse_submission, task_handler,
)
from ai_agent_engine.task_assignment import (
    AlreadyAssigned, InvalidAssignment, TaskAssignment, WorkspaceRoot,
)
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER_A = OwnerScope("owner-a", frozenset({"project-a", "project-b"}))
OWNER_B = OwnerScope("owner-b", frozenset({"project-a"}))


def submission(project_id="project-a"):
    return parse_submission(json.dumps({
        "project_id": project_id, "title": "Task", "prompt": "PRIVATE PROMPT",
    }).encode())


class WorkspaceValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ae-56-ws-")
        self.root_path = Path(self.directory.name) / "workspaces"
        self.root_path.mkdir(mode=0o700)
        (self.root_path / "slot-one").mkdir(mode=0o700)

    def tearDown(self):
        self.directory.cleanup()

    def test_trusted_private_directory_resolves_without_creation(self):
        root = WorkspaceRoot(self.root_path)
        result = root.resolve("slot-one")
        self.assertEqual(result, str(self.root_path / "slot-one"))
        self.assertEqual(stat.S_IMODE(os.stat(result).st_mode), 0o700)
        self.assertFalse((self.root_path / "not-created").exists())
        with self.assertRaises(InvalidAssignment):
            root.resolve("not-created")

    def test_rejects_traversal_absolute_paths_and_invalid_ids(self):
        root = WorkspaceRoot(self.root_path)
        for name in ("..", "../other", "/tmp", "slot/one", "", ".", "a" * 65):
            with self.subTest(name=name), self.assertRaises(InvalidAssignment):
                root.resolve(name)
        for worker in ("", "../worker", "worker/one", True, None):
            with self.subTest(worker=worker), self.assertRaises(InvalidAssignment):
                parse_assignment_request(json.dumps({
                    "worker_id": worker, "workspace_name": "slot-one",
                }).encode())

    def test_rejects_symlink_and_weak_permissions(self):
        (self.root_path / "slot-link").symlink_to(self.root_path / "slot-one")
        root = WorkspaceRoot(self.root_path)
        with self.assertRaises(InvalidAssignment):
            root.resolve("slot-link")
        os.chmod(self.root_path / "slot-one", 0o755)
        with self.assertRaises(InvalidAssignment):
            root.resolve("slot-one")
        os.chmod(self.root_path, 0o755)
        with self.assertRaises(InvalidAssignment):
            WorkspaceRoot(self.root_path)

    def test_exact_json_and_duplicate_fields(self):
        self.assertEqual(parse_assignment_request(
            b'{"worker_id":"worker-a","workspace_name":"slot-one"}'),
            ("worker-a", "slot-one"))
        for body in (
            b"{}", b"[]", b'{"worker_id":"worker-a"}',
            b'{"worker_id":"worker-a","workspace_name":"slot-one","owner_id":"owner-b"}',
            b'{"worker_id":false,"workspace_name":"slot-one"}',
        ):
            with self.subTest(body=body), self.assertRaises(InvalidAssignment):
                parse_assignment_request(body)
        with self.assertRaises(InvalidJSON):
            parse_assignment_request(
                b'{"worker_id":"a","worker_id":"b","workspace_name":"slot-one"}')


class AssignmentInboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ae-56-claim-")
        self.root_path = Path(self.directory.name) / "workspaces"
        self.root_path.mkdir(mode=0o700)
        for index in range(32):
            (self.root_path / f"slot-{index}").mkdir(mode=0o700)
        self.root = WorkspaceRoot(self.root_path)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)

    def tearDown(self):
        self.inbox.close()
        self.directory.cleanup()

    def create(self, scope=OWNER_A, project_id="project-a"):
        return self.inbox.accept(submission(project_id), scope)

    def reopen(self):
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)

    def test_first_claim_is_immutable_and_second_is_rejected(self):
        task = self.create()
        assignment = self.inbox.claim_task(task.task_id, "worker-a", "slot-0", OWNER_A)
        self.assertIsInstance(assignment, TaskAssignment)
        self.assertEqual(assignment.task_id, task.task_id)
        self.assertEqual(assignment.worker_id, "worker-a")
        self.assertEqual(assignment.workspace_dir, str(self.root_path / "slot-0"))
        with self.assertRaises(AlreadyAssigned):
            self.inbox.claim_task(task.task_id, "worker-b", "slot-1", OWNER_A)
        with self.assertRaises(AlreadyAssigned):
            self.inbox.claim_task(task.task_id, "worker-a", "slot-0", OWNER_A)
        with self.assertRaises(AttributeError):
            assignment.worker_id = "modified"
        self.assertIs(self.inbox.get_assignment(task.task_id, OWNER_A), assignment)

    def test_concurrent_32_claims_have_exactly_one_winner(self):
        task = self.create()
        def compete(index):
            try:
                return self.inbox.claim_task(task.task_id, f"worker-{index}",
                                             f"slot-{index}", OWNER_A)
            except AlreadyAssigned:
                return None
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(compete, range(32)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.inbox.get_assignment(task.task_id, OWNER_A), winners[0])
        self.reopen()
        self.assertEqual(self.inbox.get_assignment(task.task_id, OWNER_A), winners[0])

    def test_owner_project_boundaries_survive_restart(self):
        a = self.create()
        b = self.create(OWNER_B)
        c = self.create(OWNER_A, "project-b")
        with self.assertRaises(TaskNotFound):
            self.inbox.claim_task(a.task_id, "worker-b", "slot-0", OWNER_B)
        with self.assertRaises(TaskNotFound):
            self.inbox.claim_task(c.task_id, "worker-b", "slot-0", OWNER_B)
        first = self.inbox.claim_task(a.task_id, "worker-a", "slot-0", OWNER_A)
        second = self.inbox.claim_task(b.task_id, "worker-b", "slot-1", OWNER_B)
        self.assertIsNone(self.inbox.get_assignment(a.task_id, OWNER_B))
        self.reopen()
        self.assertEqual(self.inbox.get_assignment(a.task_id, OWNER_A), first)
        self.assertEqual(self.inbox.get_assignment(b.task_id, OWNER_B), second)
        self.assertIsNone(self.inbox.get_assignment(b.task_id, OWNER_A))

    def test_existing_workspace_is_required_no_directory_is_created(self):
        task = self.create()
        with self.assertRaises(InvalidAssignment):
            self.inbox.claim_task(task.task_id, "worker-a", "missing", OWNER_A)
        self.assertFalse((self.root_path / "missing").exists())
        self.assertIsNone(self.inbox.get_assignment(task.task_id, OWNER_A))

    def test_claim_requires_workspace_root(self):
        inbox = TaskInbox()
        task = inbox.accept(submission(), OWNER_A)
        with self.assertRaises(InvalidAssignment):
            inbox.claim_task(task.task_id, "worker-a", "slot-0", OWNER_A)
        self.assertIsNone(inbox.get_assignment(task.task_id, OWNER_A))

    def test_storage_failure_is_not_acknowledged_or_cached(self):
        task = self.create()
        with patch.object(self.inbox._store, "insert_assignment",
                          side_effect=StorageUnavailable("PRIVATE PROMPT")):
            with self.assertRaises(StorageUnavailable):
                self.inbox.claim_task(task.task_id, "worker-a", "slot-0", OWNER_A)
        self.assertIsNone(self.inbox.get_assignment(task.task_id, OWNER_A))
        self.reopen()
        self.assertIsNone(self.inbox.get_assignment(task.task_id, OWNER_A))

    def test_v1_db_migration_keeps_tasks(self):
        task = self.create()
        self.inbox.close()
        store = SQLiteTaskStore(self.path)
        db = store._db()
        with db:
            db.execute("DROP TABLE task_claims")
            db.execute("PRAGMA user_version=1")
        store.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)
        self.assertEqual(self.inbox.get(task.task_id, OWNER_A).receipt, task.receipt)
        self.assertIsNone(self.inbox.get_assignment(task.task_id, OWNER_A))
        assigned = self.inbox.claim_task(task.task_id, "worker-a", "slot-0", OWNER_A)
        self.reopen()
        self.assertEqual(self.inbox.get_assignment(task.task_id, OWNER_A), assigned)
        self.assertEqual(self.inbox._store._db().execute("PRAGMA user_version").fetchone(),
                         (2,))

    def test_tampered_persisted_workspace_fails_closed(self):
        task = self.create()
        self.inbox.claim_task(task.task_id, "worker-a", "slot-0", OWNER_A)
        db = self.inbox._store._db()
        with db:
            db.execute("UPDATE task_claims SET workspace_dir=? WHERE task_id=?",
                       ("/outside/root/slot-0", task.task_id))
        self.inbox.close()
        with self.assertRaises(StorageUnavailable):
            TaskInbox(store=SQLiteTaskStore(self.path), workspace_root=self.root)

    def test_same_workspace_across_different_tasks_not_yet_guarded(self):
        a, b = self.create(), self.create()
        self.inbox.claim_task(a.task_id, "worker-a", "slot-0", OWNER_A)
        # Stage 5.9 implements a shared-workspace lock, not stage 5.6.
        self.inbox.claim_task(b.task_id, "worker-b", "slot-0", OWNER_A)
        self.assertEqual(self.inbox.get_assignment(a.task_id, OWNER_A).workspace_dir,
                         self.inbox.get_assignment(b.task_id, OWNER_A).workspace_dir)

    def test_selector_skips_claimed_tasks_and_moves_to_next(self):
        first, second = self.create(), self.create()
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         first.task_id)
        self.inbox.claim_task(first.task_id, "worker-a", "slot-0", OWNER_A)
        self.assertEqual(self.inbox.select_next("project-a", OWNER_A).task_id,
                         second.task_id)
        self.inbox.claim_task(second.task_id, "worker-b", "slot-1", OWNER_A)
        self.assertIsNone(self.inbox.select_next("project-a", OWNER_A))
        self.reopen()
        self.assertIsNone(self.inbox.select_next("project-a", OWNER_A))


class ClaimHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ae-56-http-")
        self.root_path = Path(self.directory.name) / "workspaces"
        self.root_path.mkdir(mode=0o700)
        for index in range(16):
            (self.root_path / f"slot-{index}").mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(
            Path(self.directory.name) / "tasks.sqlite3"),
            workspace_root=WorkspaceRoot(self.root_path))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          task_handler(self.inbox, OWNER_A))
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.inbox.close()
        self.directory.cleanup()

    def request(self, method, path, payload=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=4)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def create(self):
        status, response = self.request("POST", "/v1/tasks", {
            "project_id": "project-a", "title": "Task", "prompt": "PRIVATE PROMPT",
        })
        self.assertEqual(status, 202)
        return response["task_id"]

    def test_http_claim_read_conflict_and_no_prompt_echo(self):
        task_id = self.create()
        path = f"/v1/tasks/{task_id}/claim"
        status, result = self.request("POST", path,
                                      {"worker_id": "worker-a", "workspace_name": "slot-0"})
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "assigned")
        self.assertEqual(result["workspace_dir"], str(self.root_path / "slot-0"))
        self.assertNotIn("PRIVATE PROMPT", json.dumps(result))
        self.assertEqual(self.request("GET", path), (200, result))
        self.assertEqual(self.request("POST", path, {
            "worker_id": "worker-b", "workspace_name": "slot-1",
        }), (409, {"status": "already_assigned"}))
        self.assertEqual(self.request("GET", path), (200, result))

    def test_http_concurrent_claims_only_one_201(self):
        task_id = self.create()
        path = f"/v1/tasks/{task_id}/claim"
        def submit(index):
            return self.request("POST", path, {
                "worker_id": f"worker-{index}", "workspace_name": f"slot-{index}",
            })
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(submit, range(16)))
        self.assertEqual([status for status, _ in results].count(201), 1)
        self.assertEqual([status for status, _ in results].count(409), 15)
        self.assertEqual(self.request("GET", path)[0], 200)

    def test_http_invalid_claims_and_foreign_scope(self):
        task_id = self.create()
        path = f"/v1/tasks/{task_id}/claim"
        self.assertEqual(self.request("GET", path), (404, {"status": "not_found"}))
        for payload in ({"worker_id": "../x", "workspace_name": "slot-0"},
                        {"worker_id": "worker-a", "workspace_name": "../slot"},
                        {"worker_id": "worker-a", "workspace_name": "slot-0",
                         "owner_id": "owner-b"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", path, payload),
                                 (422, {"status": "invalid_assignment"}))
        self.assertEqual(self.request("POST", path, {
            "worker_id": "worker-a", "workspace_name": "nonexistent",
        }), (422, {"status": "invalid_workspace"}))
        self.assertEqual(self.request("POST", "/v1/tasks/task_" + "0"*32 + "/claim", {
            "worker_id": "worker-a", "workspace_name": "slot-0",
        }), (404, {"status": "not_found"}))
        foreign_server = ThreadingHTTPServer(("127.0.0.1", 0),
                                             task_handler(self.inbox, OWNER_B))
        foreign_thread = Thread(target=foreign_server.serve_forever, daemon=True)
        foreign_thread.start()
        try:
            foreign_port = foreign_server.server_address[1]
            self.assertEqual(self.request("POST", path, {
                "worker_id": "worker-b", "workspace_name": "slot-0",
            }, port=foreign_port), (404, {"status": "not_found"}))
            self.assertEqual(self.request("GET", path, port=foreign_port),
                             (404, {"status": "not_found"}))
        finally:
            foreign_server.shutdown()
            foreign_server.server_close()
            foreign_thread.join(timeout=3)

    def test_http_storage_failure_returns_safe_503(self):
        task_id = self.create()
        path = f"/v1/tasks/{task_id}/claim"
        with patch.object(self.inbox._store, "insert_assignment",
                          side_effect=StorageUnavailable("PRIVATE PROMPT")):
            self.assertEqual(self.request("POST", path, {
                "worker_id": "worker-a", "workspace_name": "slot-0",
            }), (503, {"status": "storage_unavailable"}))
        self.assertEqual(self.request("GET", path), (404, {"status": "not_found"}))


class ClaimProcessRestartTests(unittest.TestCase):
    def test_live_http_claim_survives_process_restart(self):
        with tempfile.TemporaryDirectory(prefix="ae-56-process-") as root:
            path = Path(root) / "tasks.sqlite3"
            workspace_root = Path(root) / "workspaces"
            workspace_root.mkdir(mode=0o700)
            (workspace_root / "slot-one").mkdir(mode=0o700)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            def exchange(method, endpoint, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    payload = json.dumps(body).encode() if body is not None else None
                    conn.request(method, endpoint, body=payload,
                                 headers={"Content-Type": "application/json"})
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    conn.close()
            def launch():
                return subprocess.Popen([
                    sys.executable, "-m", "ai_agent_engine.task_api",
                    "--port", str(port), "--owner-id", "owner-a",
                    "--project-id", "project-a", "--db", str(path),
                    "--workspace-root", str(workspace_root),
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def ready(process):
                end = time.monotonic() + 8
                while time.monotonic() < end:
                    if process.poll() is not None:
                        self.fail(f"claim server terminated: {process.returncode}")
                    try:
                        exchange("GET", "/v1/projects/project-a/next-task")
                        return
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.05)
                self.fail("claim server did not start")
            process = launch()
            try:
                ready(process)
                status, posted = exchange("POST", "/v1/tasks", {
                    "project_id": "project-a", "title": "test", "prompt": "secret",
                })
                self.assertEqual(status, 202)
                claim_path = f"/v1/tasks/{posted['task_id']}/claim"
                status, original = exchange("POST", claim_path, {
                    "worker_id": "worker-a", "workspace_name": "slot-one",
                })
                self.assertEqual(status, 201)
                process.terminate()
                process.wait(timeout=5)
                process = launch()
                ready(process)
                self.assertEqual(exchange("GET", claim_path), (200, original))
                self.assertEqual(exchange("POST", claim_path, {
                    "worker_id": "worker-b", "workspace_name": "slot-one",
                }), (409, {"status": "already_assigned"}))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
