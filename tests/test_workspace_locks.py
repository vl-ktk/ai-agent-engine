"""Stage 5.9: exclusive active workspace locks for different tasks/processes."""

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import (
    OwnerScope, TaskInbox, parse_submission, task_handler,
)
from ai_agent_engine.task_assignment import (
    InvalidAssignment, WorkspaceBusy, WorkspaceRoot,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
OTHER_OWNER = OwnerScope("owner-b", frozenset({"project-a"}))


def submission():
    return parse_submission(json.dumps({
        "project_id": "project-a", "title": "Private title",
        "prompt": "CONFIDENTIAL PROMPT",
    }).encode())


class WorkspaceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-59-")
        self.base = Path(self.temp.name)
        self.workspaces = self.base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        (self.workspaces / "slot-a").mkdir(mode=0o700)
        (self.workspaces / "slot-b").mkdir(mode=0o700)
        self.root = WorkspaceRoot(self.workspaces)
        self.db = self.base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def create(self, scope=OWNER):
        return self.inbox.accept(submission(), scope).task_id

    def claim(self, task_id, workspace="slot-a", worker="worker-a", scope=OWNER):
        return self.inbox.claim_task(task_id, worker, workspace, scope)

    def reopen(self):
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)

    def test_directory_lock_is_exclusive_without_lockfiles_or_chmod(self):
        before = tuple(self.workspaces.iterdir())
        lease = self.root.acquire("slot-a")
        try:
            with self.assertRaises(WorkspaceBusy):
                WorkspaceRoot(self.workspaces).acquire("slot-a")
            independent = self.root.acquire("slot-b")
            independent.close()
            self.assertEqual(tuple(self.workspaces.iterdir()), before)
            self.assertEqual(list((self.workspaces / "slot-a").iterdir()), [])
        finally:
            lease.close()
        new_lease = self.root.acquire("slot-a")
        new_lease.close()
        new_lease.close()  # safe repeat release

    def test_two_different_tasks_same_workspace_cannot_be_assigned(self):
        first, second = self.create(), self.create()
        original = self.claim(first)
        with self.assertRaises(WorkspaceBusy):
            self.claim(second, worker="worker-b")
        self.assertEqual(self.inbox.state(second, OWNER), TaskState.QUEUED)
        self.assertIsNone(self.inbox.get_assignment(second, OWNER))
        event = self.inbox.transition_history(second, OWNER)[-1]
        self.assertFalse(event.accepted)
        self.assertEqual(event.reason_code, "workspace_busy")
        self.assertEqual(event.to_state, TaskState.ASSIGNED)
        self.assertEqual(self.inbox.get_assignment(first, OWNER), original)

    def test_32_different_tasks_contend_for_one_directory(self):
        ids = [self.create() for _ in range(32)]

        def compete(item):
            index, task_id = item
            try:
                return self.claim(task_id, worker=f"worker-{index}") is not None
            except WorkspaceBusy:
                return False

        with ThreadPoolExecutor(max_workers=16) as pool:
            result = list(pool.map(compete, enumerate(ids)))
        self.assertEqual(result.count(True), 1)
        self.assertEqual(result.count(False), 31)
        for task_id, won in zip(ids, result):
            self.assertEqual(self.inbox.state(task_id, OWNER),
                             TaskState.ASSIGNED if won else TaskState.QUEUED)
            events = self.inbox.transition_history(task_id, OWNER)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].accepted, won)
        self.reopen()
        self.assertEqual(sum(self.inbox.state(t, OWNER) is TaskState.ASSIGNED
                             for t in ids), 1)

    def test_all_terminal_states_release_workspace_for_new_task(self):
        for terminal in (TaskState.SUCCEEDED, TaskState.FAILED,
                         TaskState.CANCELLED, TaskState.TIMED_OUT):
            with self.subTest(terminal=terminal):
                task_id = self.create()
                self.claim(task_id)
                if terminal is TaskState.SUCCEEDED:
                    self.inbox.transition_state(task_id, TaskState.RUNNING,
                                                OWNER, actor_id="worker-a")
                self.inbox.transition_state(task_id, terminal,
                                            OWNER, actor_id="worker-a")
                lease = self.root.acquire("slot-a")
                lease.close()
                self.reopen()
                self.assertIs(self.inbox.state(task_id, OWNER), terminal)
                next_task = self.create()
                self.claim(next_task)
                self.inbox.transition_state(next_task, TaskState.CANCELLED,
                                            OWNER, actor_id="worker-a")

    def test_active_lease_is_restored_after_restart_and_terminal_is_not(self):
        task_id = self.create()
        self.claim(task_id)
        self.inbox.close()
        # No process owns the directory while the inbox is stopped.
        lease = self.root.acquire("slot-a")
        lease.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        with self.assertRaises(WorkspaceBusy):
            self.root.acquire("slot-a")
        self.inbox.transition_state(task_id, TaskState.FAILED,
                                    OWNER, actor_id="worker-a")
        lease = self.root.acquire("slot-a")
        lease.close()
        self.reopen()
        lease = self.root.acquire("slot-a")
        lease.close()

    def test_active_restoration_requires_configured_root(self):
        task_id = self.create()
        self.claim(task_id)
        self.inbox.close()
        with self.assertRaises(StorageUnavailable):
            TaskInbox(store=SQLiteTaskStore(self.db))
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)

    def test_restore_fails_closed_when_another_process_holds_directory(self):
        task_id = self.create()
        self.claim(task_id)
        self.inbox.close()
        external = self.root.acquire("slot-a")
        try:
            with self.assertRaises(StorageUnavailable):
                TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        finally:
            external.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)

    def test_old_conflicting_persisted_claims_fail_closed_without_leak(self):
        first, second = self.create(), self.create()
        self.claim(first)
        self.inbox.close()
        store = SQLiteTaskStore(self.db)
        with store._db() as db:
            db.execute("""INSERT INTO task_claims
                (task_id,worker_id,workspace_name,workspace_dir)
                VALUES (?,?,?,?)""", (second, "worker-b", "slot-a",
                                       str(self.workspaces / "slot-a")))
            db.execute("UPDATE task_states SET state='assigned' WHERE task_id=?",
                       (second,))
        store.close()
        with self.assertRaises(StorageUnavailable):
            TaskInbox(store=SQLiteTaskStore(self.db), workspace_root=self.root)
        # Constructor must release its first successful lock on failure.
        lease = self.root.acquire("slot-a")
        lease.close()

    def test_independent_databases_share_the_directory_lock(self):
        first = self.create()
        self.claim(first)
        other = TaskInbox(store=SQLiteTaskStore(self.base / "other.sqlite3"),
                          workspace_root=WorkspaceRoot(self.workspaces))
        try:
            task_id = other.accept(submission(), OTHER_OWNER).task_id
            with self.assertRaises(WorkspaceBusy):
                other.claim_task(task_id, "worker-b", "slot-a", OTHER_OWNER)
            self.assertEqual(other.state(task_id, OTHER_OWNER), TaskState.QUEUED)
            self.inbox.transition_state(first, TaskState.CANCELLED,
                                        OWNER, actor_id="worker-a")
            other.claim_task(task_id, "worker-b", "slot-a", OTHER_OWNER)
        finally:
            other.close()

    def test_different_workspace_names_can_be_claimed_independently(self):
        first, second = self.create(), self.create()
        self.claim(first)
        self.claim(second, workspace="slot-b", worker="worker-b")
        self.assertIs(self.inbox.state(first, OWNER), TaskState.ASSIGNED)
        self.assertIs(self.inbox.state(second, OWNER), TaskState.ASSIGNED)

    def test_storage_error_releases_uncommitted_workspace_lock(self):
        task_id = self.create()
        with patch.object(self.inbox._store, "insert_assignment",
                          side_effect=StorageUnavailable("HIDDEN")):
            with self.assertRaises(StorageUnavailable):
                self.claim(task_id)
        lease = self.root.acquire("slot-a")
        lease.close()
        self.assertIsNone(self.inbox.get_assignment(task_id, OWNER))
        self.assertIs(self.inbox.state(task_id, OWNER), TaskState.QUEUED)
        self.claim(task_id)

    def test_lock_exists_during_database_commit_before_ack(self):
        task_id = self.create()
        in_transaction = Event()
        continue_transaction = Event()
        result = {}
        original = self.inbox._store.insert_assignment

        def delayed(assignment):
            in_transaction.set()
            self.assertTrue(continue_transaction.wait(5))
            return original(assignment)

        def start_claim():
            try:
                result["assignment"] = self.claim(task_id)
            except Exception as exc:
                result["error"] = exc

        with patch.object(self.inbox._store, "insert_assignment", side_effect=delayed):
            thread = Thread(target=start_claim)
            thread.start()
            try:
                self.assertTrue(in_transaction.wait(5))
                with self.assertRaises(WorkspaceBusy):
                    self.root.acquire("slot-a")
            finally:
                continue_transaction.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["assignment"].task_id, task_id)

    def test_symlink_and_invalid_permissions_never_acquire(self):
        (self.workspaces / "bad-link").symlink_to(self.workspaces / "slot-a")
        with self.assertRaises(InvalidAssignment):
            self.root.acquire("bad-link")
        other = self.workspaces / "slot-b"
        other.chmod(0o755)
        with self.assertRaises(InvalidAssignment):
            self.root.acquire("slot-b")
        with self.assertRaises(InvalidAssignment):
            self.root.acquire("../slot-a")

    def test_real_os_subprocess_conflicts_and_releases_on_exit(self):
        source = ("import sys\n"
                  "from ai_agent_engine.task_assignment import WorkspaceRoot\n"
                  "lease=WorkspaceRoot(sys.argv[1]).acquire('slot-a')\n"
                  "print('LOCKED', flush=True)\n"
                  "sys.stdin.readline()\n"
                  "lease.close()\n")
        process = subprocess.Popen(
            [sys.executable, "-c", source, str(self.workspaces)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5),
                                "child process did not acquire its lock")
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            with self.assertRaises(WorkspaceBusy):
                self.root.acquire("slot-a")
            output, errors = process.communicate("go\n", timeout=5)
            self.assertEqual((process.returncode, output, errors), (0, "", ""))
            lease = self.root.acquire("slot-a")
            lease.close()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


class WorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-59-http-")
        root_path = Path(self.temp.name) / "workspaces"
        root_path.mkdir(mode=0o700)
        (root_path / "slot-a").mkdir(mode=0o700)
        self.inbox = TaskInbox(
            store=SQLiteTaskStore(Path(self.temp.name) / "tasks.sqlite3"),
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
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def create(self):
        status, body = self.request("POST", "/v1/tasks", {
            "project_id": "project-a", "title": "Test", "prompt": "HIDDEN",
        })
        self.assertEqual(status, 202)
        return body["task_id"]

    def test_16_http_clients_different_tasks_one_workspace(self):
        ids = [self.create() for _ in range(16)]

        def compete(item):
            index, task_id = item
            return self.request("POST", f"/v1/tasks/{task_id}/claim", {
                "worker_id": f"worker-{index}", "workspace_name": "slot-a",
            })

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(compete, enumerate(ids)))
        self.assertEqual(sum(status == 201 for status, _ in results), 1)
        self.assertEqual(sum(status == 409 for status, _ in results), 15)
        self.assertEqual([body["status"] for status, body in results
                          if status == 409], ["workspace_busy"] * 15)
        for task_id, (status, _) in zip(ids, results):
            events = self.inbox.transition_history(task_id, OWNER)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].accepted, status == 201)
            self.assertNotIn("HIDDEN", str(events))

    def test_http_busy_then_retry_after_terminal(self):
        first, second = self.create(), self.create()
        path_a = f"/v1/tasks/{first}/claim"
        path_b = f"/v1/tasks/{second}/claim"
        original = {"worker_id": "worker-a", "workspace_name": "slot-a"}
        contender = {"worker_id": "worker-b", "workspace_name": "slot-a"}
        self.assertEqual(self.request("POST", path_a, original)[0], 201)
        self.assertEqual(self.request("POST", path_b, contender),
                         (409, {"status": "workspace_busy"}))
        # A trusted internal terminal event frees the OS lease.
        self.inbox.transition_state(first, TaskState.CANCELLED, OWNER,
                                    actor_id="worker-a")
        self.assertEqual(self.request("POST", path_b, contender)[0], 201)
