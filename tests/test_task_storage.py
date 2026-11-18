"""Stage 5.5: private SQLite inbox survives close, restart and abrupt stop."""

import hashlib
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
    DependencyNotFound, InboxFull, OwnerScope, TaskInbox,
    parse_submission, task_handler,
)
from ai_agent_engine.task_dag import DependencyCycle
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a", "project-b"}))
OTHER = OwnerScope("owner-b", frozenset({"project-a"}))


def submission(project_id="project-a", *, priority=0, parent_task_id=None):
    payload = {"project_id": project_id, "title": "Durable task",
               "prompt": "Secret prompt: do not return over HTTP", "priority": priority}
    if parent_task_id is not None:
        payload["parent_task_id"] = parent_task_id
    return parse_submission(json.dumps(payload).encode())


class PersistentInboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ae-55-")
        self.path = Path(self.directory.name) / "inbox.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path))

    def tearDown(self):
        self.inbox.close()
        self.directory.cleanup()

    def reopen(self, *, max_pending=128):
        self.inbox.close()
        self.inbox = TaskInbox(max_pending=max_pending,
                               store=SQLiteTaskStore(self.path))

    def test_private_file_and_recovered_record_ids(self):
        parent = self.inbox.accept(submission(priority=2), OWNER)
        child = self.inbox.accept(submission(priority=5,
                     parent_task_id=parent.task_id), OWNER)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        self.reopen()
        restored_parent = self.inbox.get(parent.task_id, OWNER)
        restored_child = self.inbox.get(child.task_id, OWNER)
        self.assertEqual(restored_parent.receipt, parent.receipt)
        self.assertEqual(restored_parent.submission.prompt, parent.submission.prompt)
        self.assertEqual(restored_child.parent_task_id, parent.task_id)
        self.assertEqual(self.inbox.priority(child.task_id, OWNER), 5)
        self.assertIsNone(self.inbox.get(parent.task_id, OTHER))
        next_record = self.inbox.accept(submission(), OWNER)
        self.assertGreater(next_record.receipt, child.receipt)
        self.assertEqual(len(self.inbox.snapshot(OWNER)), 3)

    def test_dependencies_priorities_and_order_survive_reopen(self):
        a = self.inbox.accept(submission(priority=100), OWNER)
        b = self.inbox.accept(submission(priority=1), OWNER)
        c = self.inbox.accept(submission(priority=50), OWNER)
        self.inbox.set_dependencies(a.task_id, (b.task_id,), OWNER)
        self.inbox.set_dependencies(c.task_id, (a.task_id,), OWNER)
        self.inbox.set_priority(c.task_id, 90, OWNER)
        self.reopen()
        self.assertEqual(self.inbox.dependencies(a.task_id, OWNER), (b.task_id,))
        self.assertEqual(self.inbox.dependencies(c.task_id, OWNER), (a.task_id,))
        self.assertEqual(self.inbox.priority(c.task_id, OWNER), 90)
        self.assertEqual(self.inbox.topological_order("project-a", OWNER),
                         (b.task_id, a.task_id, c.task_id))
        self.assertEqual(self.inbox.select_next("project-a", OWNER).task_id,
                         b.task_id)
        self.assertEqual(self.inbox.select_next("project-a", OWNER,
                         completed_task_ids=(b.task_id,)).task_id, a.task_id)
        self.assertEqual(self.inbox.select_next("project-a", OWNER,
                         completed_task_ids=(a.task_id, b.task_id)).task_id, c.task_id)

    def test_shared_db_owner_project_boundaries_survive_reopen(self):
        a = self.inbox.accept(submission(), OWNER)
        b = self.inbox.accept(submission(), OTHER)
        c = self.inbox.accept(submission("project-b"), OWNER)
        self.reopen()
        self.assertEqual({record.task_id for record in self.inbox.snapshot(OWNER)},
                         {a.task_id, c.task_id})
        self.assertEqual({record.task_id for record in self.inbox.snapshot(OTHER)},
                         {b.task_id})
        for other_id in (b.task_id, c.task_id):
            with self.subTest(other_id=other_id), self.assertRaises(DependencyNotFound):
                self.inbox.set_dependencies(a.task_id, (other_id,), OWNER)

    def test_invalid_cycle_does_not_persist(self):
        a = self.inbox.accept(submission(), OWNER)
        b = self.inbox.accept(submission(), OWNER)
        self.inbox.set_dependencies(b.task_id, (a.task_id,), OWNER)
        with self.assertRaises(DependencyCycle):
            self.inbox.set_dependencies(a.task_id, (b.task_id,), OWNER)
        self.reopen()
        self.assertEqual(self.inbox.dependencies(a.task_id, OWNER), ())
        self.assertEqual(self.inbox.dependencies(b.task_id, OWNER), (a.task_id,))

    def test_reopened_capacity_is_enforced_without_loss(self):
        a = self.inbox.accept(submission(), OWNER)
        self.reopen(max_pending=1)
        with self.assertRaises(InboxFull):
            self.inbox.accept(submission(), OWNER)
        self.assertEqual(self.inbox.snapshot(OWNER)[0].task_id, a.task_id)

    def test_insert_failure_does_not_acknowledge_or_change_memory(self):
        with patch.object(self.inbox._store, "insert_task",
                          side_effect=StorageUnavailable("injected")):
            with self.assertRaises(StorageUnavailable):
                self.inbox.accept(submission(), OWNER)
        self.assertEqual(self.inbox.snapshot(OWNER), ())
        self.reopen()
        self.assertEqual(self.inbox.snapshot(OWNER), ())
        self.assertEqual(self.inbox.accept(submission(), OWNER).receipt, 1)

    def test_priority_persist_failure_keeps_old_value(self):
        a = self.inbox.accept(submission(priority=12), OWNER)
        with patch.object(self.inbox._store, "update_priority",
                          side_effect=StorageUnavailable("injected")):
            with self.assertRaises(StorageUnavailable):
                self.inbox.set_priority(a.task_id, 99, OWNER)
        self.assertEqual(self.inbox.priority(a.task_id, OWNER), 12)
        self.reopen()
        self.assertEqual(self.inbox.priority(a.task_id, OWNER), 12)

    def test_http_storage_failure_is_sanitized_and_does_not_acknowledge(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     task_handler(self.inbox, OWNER))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body):
            conn = http.client.HTTPConnection("127.0.0.1",
                server.server_address[1], timeout=3)
            try:
                conn.request(method, path, body=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        try:
            with patch.object(self.inbox._store, "insert_task",
                              side_effect=StorageUnavailable("PRIVATE-SECRET")):
                self.assertEqual(request("POST", "/v1/tasks", {
                    "project_id": "project-a", "title": "test", "prompt": "PRIVATE-SECRET",
                }), (503, {"status": "storage_unavailable"}))
            self.assertEqual(self.inbox.snapshot(OWNER), ())
            a = self.inbox.accept(submission(), OWNER)
            b = self.inbox.accept(submission(), OWNER)
            with patch.object(self.inbox._store, "replace_dependencies",
                              side_effect=StorageUnavailable("PRIVATE-SECRET")):
                self.assertEqual(request("PUT", f"/v1/tasks/{a.task_id}/dependencies",
                    {"depends_on": [b.task_id]}),
                    (503, {"status": "storage_unavailable"}))
            with patch.object(self.inbox._store, "update_priority",
                              side_effect=StorageUnavailable("PRIVATE-SECRET")):
                self.assertEqual(request("PUT", f"/v1/tasks/{a.task_id}/priority",
                    {"priority": 100}), (503, {"status": "storage_unavailable"}))
            self.assertEqual(self.inbox.dependencies(a.task_id, OWNER), ())
            self.assertEqual(self.inbox.priority(a.task_id, OWNER), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.reopen()
        self.assertEqual(self.inbox.dependencies(a.task_id, OWNER), ())
        self.assertEqual(self.inbox.priority(a.task_id, OWNER), 0)

    def test_sql_transaction_rolls_back_failed_edge_replacement(self):
        a = self.inbox.accept(submission(), OWNER)
        b = self.inbox.accept(submission(), OWNER)
        c = self.inbox.accept(submission(), OWNER)
        self.inbox.set_dependencies(a.task_id, (b.task_id,), OWNER)
        db = self.inbox._store._db()
        db.execute("""CREATE TRIGGER block_dependency_insert BEFORE INSERT
            ON dependencies BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        try:
            with self.assertRaises(StorageUnavailable):
                self.inbox.set_dependencies(a.task_id, (c.task_id,), OWNER)
            self.assertEqual(self.inbox.dependencies(a.task_id, OWNER), (b.task_id,))
        finally:
            db.execute("DROP TRIGGER block_dependency_insert")
        self.reopen()
        self.assertEqual(self.inbox.dependencies(a.task_id, OWNER), (b.task_id,))

    def test_exclusive_writer_lock_and_clean_reopen(self):
        with self.assertRaises(StorageUnavailable):
            SQLiteTaskStore(self.path)
        self.reopen()
        self.assertIsNotNone(self.inbox._store)

    def test_corrupt_cross_scope_edge_rejected_on_restart(self):
        a = self.inbox.accept(submission(), OWNER)
        b = self.inbox.accept(submission(), OTHER)
        db = self.inbox._store._db()
        db.execute("INSERT INTO dependencies (task_id,prerequisite_id) VALUES (?,?)",
                   (a.task_id, b.task_id))
        db.commit()
        self.inbox.close()
        with self.assertRaises(StorageUnavailable):
            TaskInbox(store=SQLiteTaskStore(self.path))
        # The failed load does not delete or silently rewrite the DB.
        self.assertTrue(self.path.exists())


class StorageSafetyTests(unittest.TestCase):
    def test_refuses_world_accessible_parent_directory(self):
        with tempfile.TemporaryDirectory(prefix="ae-55-perm-") as root:
            os.chmod(root, 0o755)
            with self.assertRaises(StorageUnavailable):
                SQLiteTaskStore(Path(root) / "private.sqlite3")
            self.assertFalse((Path(root) / "private.sqlite3").exists())

    def test_refuses_symlinks_and_readable_database(self):
        with tempfile.TemporaryDirectory(prefix="ae-55-perm-") as root:
            target = Path(root) / "ordinary"
            target.write_text("not a database")
            os.chmod(target, 0o600)
            link = Path(root) / "symlink.sqlite3"
            link.symlink_to(target)
            with self.assertRaises(StorageUnavailable):
                SQLiteTaskStore(link)
            self.assertEqual(target.read_text(), "not a database")
            open_file = Path(root) / "readable.sqlite3"
            open_file.touch(mode=0o644)
            with self.assertRaises(StorageUnavailable):
                SQLiteTaskStore(open_file)

    def test_refuses_unrelated_existing_sqlite_database(self):
        with tempfile.TemporaryDirectory(prefix="ae-55-other-") as root:
            path = Path(root) / "existing.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE unrelated (value TEXT)")
            os.chmod(path, 0o600)
            original_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(StorageUnavailable):
                SQLiteTaskStore(path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             original_digest)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='unrelated'").fetchone(),
                                 ("unrelated",))


class ProcessRestartHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ae-55-http-")
        self.path = Path(self.directory.name) / "queue.sqlite3"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.process = None

    def tearDown(self):
        self.stop_process()
        self.directory.cleanup()

    def start_process(self):
        self.process = subprocess.Popen([
            sys.executable, "-m", "ai_agent_engine.task_api",
            "--port", str(self.port), "--owner-id", "owner-a",
            "--project-id", "project-a", "--db", str(self.path),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if self.process.poll() is not None:
                self.fail(f"durable process exited: {self.process.returncode}")
            try:
                self.request("GET", "/v1/projects/project-a/next-task")
                return
            except (OSError, http.client.HTTPException):
                time.sleep(0.05)
        self.fail("durable loopback HTTP listener did not start")

    def stop_process(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            data = json.dumps(payload).encode() if payload is not None else None
            conn.request(method, path, body=data,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_real_process_restart_recovers_http_records_and_graph(self):
        self.start_process()
        status, a = self.request("POST", "/v1/tasks", {
            "project_id": "project-a", "title": "first", "prompt": "sensitive",
            "priority": 2,
        })
        self.assertEqual(status, 202)
        self.assertEqual(a["status"], "accepted_durable")
        status, b = self.request("POST", "/v1/tasks", {
            "project_id": "project-a", "title": "child", "prompt": "sensitive",
            "parent_task_id": a["task_id"], "priority": 90,
        })
        self.assertEqual(status, 202)
        self.assertEqual(b["status"], "accepted_durable")
        self.assertEqual(self.request("PUT", f"/v1/tasks/{b['task_id']}/dependencies",
                          {"depends_on": [a["task_id"]]})[0], 200)
        self.assertEqual(self.request("PUT", f"/v1/tasks/{a['task_id']}/priority",
                          {"priority": 50})[0], 200)
        self.stop_process()  # SIGTERM: no graceful app shutdown required.
        self.start_process()
        status, read_a = self.request("GET", f"/v1/tasks/{a['task_id']}")
        self.assertEqual((status, read_a["task_id"], read_a["priority"]),
                         (200, a["task_id"], 50))
        status, read_b = self.request("GET", f"/v1/tasks/{b['task_id']}")
        self.assertEqual((status, read_b["parent_task_id"], read_b["depends_on"]),
                         (200, a["task_id"], [a["task_id"]]))
        self.assertNotIn("sensitive", json.dumps(read_b))
        self.assertEqual(self.request("GET", "/v1/projects/project-a/task-order")[1]["task_ids"],
                         [a["task_id"], b["task_id"]])
        self.assertEqual(self.request("GET", "/v1/projects/project-a/next-task")[1]["task_id"],
                         a["task_id"])
        status, c = self.request("POST", "/v1/tasks", {
            "project_id": "project-a", "title": "after restart", "prompt": "test",
        })
        self.assertEqual(status, 202)
        self.assertGreater(c["receipt"], b["receipt"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
