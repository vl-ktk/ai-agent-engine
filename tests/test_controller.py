"""Offline tests for the real persistent controller lifecycle (no paid APIs/VPS)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from ai_agent_engine.controller import (
    ControllerError, ControllerStore, LEASE_SECONDS, run_core_self_test,
    private_directory,
)


class ControllerStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.state_dir.mkdir(mode=0o700)
        self.store = ControllerStore(self.state_dir)

    def connection(self):
        conn = self.store.connect()
        self.addCleanup(conn.close)
        return conn

    def test_self_test_exercises_actual_core(self):
        self.assertTrue(run_core_self_test())

    def test_private_state_directory_is_required(self):
        self.assertEqual(private_directory(self.state_dir), self.state_dir.resolve())
        link = Path(self.temporary.name) / "link"
        try:
            link.symlink_to(self.state_dir, target_is_directory=True)
        except OSError:
            pass  # Windows may deny symlinks without Developer Mode.
        else:
            with self.assertRaises(ControllerError):
                ControllerStore(link)
        if os.name == "posix":
            self.state_dir.chmod(0o755)
            with self.assertRaises(ControllerError):
                ControllerStore(self.state_dir)

    def test_enqueue_persists_and_deduplicates(self):
        conn = self.connection()
        first = self.store.enqueue(conn, "core_self_test", "test-key")
        duplicate = self.store.enqueue(conn, "core_self_test", "test-key")
        self.assertEqual(first, duplicate)
        self.assertEqual(self.store.inspect()["jobs"]["pending"], 1)
        with self.assertRaises(ControllerError):
            self.store.enqueue(conn, "shell", "bad")
        with self.assertRaises(ControllerError):
            self.store.enqueue(conn, "core_self_test", "../escape")

    def test_singleton_lease_and_pending_job_recovery(self):
        conn = self.connection()
        self.store.enqueue(conn, "core_self_test", "retry-key")
        self.store.acquire(conn, "first-instance")
        with self.assertRaises(ControllerError):
            self.store.acquire(conn, "second-instance")
        self.assertEqual(self.store.claim(conn), ("retry-key", "core_self_test"))
        self.assertEqual(self.store.inspect()["jobs"]["running"], 1)
        conn.execute("UPDATE singleton SET lease_until=0 WHERE id=1")
        self.store.acquire(conn, "second-instance")
        self.assertEqual(self.store.inspect()["jobs"]["pending"], 1)
        claimed = self.store.claim(conn)
        self.assertEqual(claimed, ("retry-key", "core_self_test"))
        self.store.finish(conn, claimed[0], run_core_self_test())
        self.store.release(conn, "second-instance")
        state = self.store.inspect()
        self.assertEqual(state["jobs"]["done"], 1)
        self.assertFalse(state["active"])

    def test_stale_running_jobs_stop_after_retry_limit(self):
        conn = self.connection()
        conn.execute(
            "INSERT INTO jobs(id,kind,state,attempts,created,updated) "
            "VALUES('old','core_self_test','running',?,?,?)",
            (3, time.time(), time.time())
        )
        self.store.acquire(conn, "fresh")
        self.assertEqual(self.store.inspect()["jobs"]["failed"], 1)
        self.store.release(conn, "fresh")

    def test_status_never_creates_state(self):
        with self.assertRaises(ControllerError):
            self.store.inspect()
        self.assertFalse(self.store.database.exists())


class ControllerCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.state_dir.mkdir(mode=0o700)

    def call(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ai_agent_engine.controller", *args,
             "--state-dir", str(self.state_dir)],
            capture_output=True, text=True, timeout=15,
        )

    @unittest.skipIf(os.name == "posix" and os.geteuid() == 0,
                     "root must not run the controller")
    def test_enqueue_run_once_and_status_across_processes(self):
        queued = self.call("enqueue", "--idempotency-key", "process-test")
        self.assertEqual(queued.returncode, 0, queued.stderr)
        self.assertEqual(json.loads(queued.stdout)["job_id"], "process-test")
        run = self.call("run-once")
        self.assertEqual(run.returncode, 0, run.stderr)
        report = self.call("status")
        self.assertEqual(report.returncode, 0, report.stderr)
        state = json.loads(report.stdout)
        self.assertEqual(state["jobs"]["done"], 1)
        self.assertFalse(state["active"])
        self.assertNotIn("probe.txt", report.stdout)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "graceful SIGTERM lifecycle is exercised on non-root POSIX CI")
    def test_background_service_heartbeat_and_graceful_shutdown(self):
        cmd = [
            sys.executable, "-m", "ai_agent_engine.controller", "serve",
            "--state-dir", str(self.state_dir), "--poll-interval", "0.1",
        ]
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        ready = False
        for _ in range(40):
            health = self.call("health")
            if health.returncode == 0:
                ready = True
                break
            if child.poll() is not None:
                break
            time.sleep(0.1)
        self.assertTrue(ready, f"controller failed to become healthy: rc={child.poll()}")
        queued = self.call("enqueue", "--idempotency-key", "live-probe")
        self.assertEqual(queued.returncode, 0, queued.stderr)
        finished = False
        for _ in range(40):
            report = self.call("status")
            if report.returncode == 0 and json.loads(report.stdout)["jobs"]["done"] == 1:
                finished = True
                break
            time.sleep(0.1)
        self.assertTrue(finished, "Controller did not process persisted queued job")
        child.terminate()
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertIn("controller_started", out)
        self.assertIn("job_completed", out)
        self.assertIn("controller_stopped", out)
        self.assertEqual(self.call("health").returncode, 1)


if __name__ == "__main__":
    unittest.main()
