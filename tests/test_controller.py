"""Offline lifecycle checks. No VPS, network ports, models or credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from ai_agent_engine.controller import check_health, main


class ControllerHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "health.json"

    def test_missing_corrupt_stale_and_future_heartbeat_rejected(self):
        self.assertFalse(check_health(self.file))
        self.file.write_text("not json")
        self.assertFalse(check_health(self.file))
        data = {"schema": 1, "service": "ai-agent-engine-controller", "status": "ready",
                "pid": os.getpid(), "heartbeat_unix": time.time() - 60}
        self.file.write_text(json.dumps(data))
        self.assertFalse(check_health(self.file, max_age=10))
        data["heartbeat_unix"] = time.time() + 60
        self.file.write_text(json.dumps(data))
        self.assertFalse(check_health(self.file))
        data["heartbeat_unix"] = time.time()
        data["pid"] = True
        self.file.write_text(json.dumps(data))
        self.assertFalse(check_health(self.file))
        data["pid"] = os.getpid()
        self.file.write_text(json.dumps(data))
        self.assertEqual(check_health(self.file), os.name == "posix")

    def test_cli_check_fails_closed(self):
        self.assertEqual(main(["check", "--health-file", str(self.file)]), 1)
        with self.assertRaises(ValueError):
            check_health(self.file, max_age=0)

    @unittest.skipUnless(os.name == "posix", "Persistent systemd controller is POSIX-only")
    def test_real_process_heartbeat_health_lock_and_graceful_stop(self):
        arguments = [sys.executable, "-m", "ai_agent_engine.controller", "run",
                     "--health-file", str(self.file), "--interval", "0.1"]
        first = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._stop, first)
        deadline = time.monotonic() + 5
        while not check_health(self.file) and time.monotonic() < deadline:
            if first.poll() is not None:
                self.fail("Controller exited before readiness")
            time.sleep(0.03)
        self.assertTrue(check_health(self.file))
        assert first.pid is not None
        self.assertEqual(json.loads(self.file.read_text())["pid"], first.pid)
        second = subprocess.run(arguments, capture_output=True, timeout=3)
        self.assertNotEqual(second.returncode, 0, "Single instance lock must refuse overlap")
        self.assertTrue(check_health(self.file))
        first.send_signal(signal.SIGTERM)
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertFalse(self.file.exists(), "Graceful exit must invalidate health")
        third = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._stop, third)
        deadline = time.monotonic() + 5
        while not check_health(self.file) and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertTrue(check_health(self.file), "Kernel should release flock after exit")

    @unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"),
                         "systemd notification uses a Unix datagram socket")
    def test_systemd_receives_ready_and_watchdog_notifications(self):
        notify_path = str(Path(self.temp.name) / "notify.sock")
        try:
            receiver_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except PermissionError:
            self.skipTest("Execution sandbox forbids Unix datagram sockets")
        with receiver_socket as receiver:
            receiver.bind(notify_path)
            receiver.settimeout(5)
            env = os.environ.copy()
            env["NOTIFY_SOCKET"] = notify_path
            process = subprocess.Popen(
                [sys.executable, "-m", "ai_agent_engine.controller", "run",
                 "--health-file", str(self.file), "--interval", "0.1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            self.addCleanup(self._stop, process)
            self.assertIn(b"READY=1", receiver.recv(4096))
            self.assertTrue(check_health(self.file))
            self.assertIn(b"WATCHDOG=1", receiver.recv(4096))
            process.send_signal(signal.SIGTERM)
            self.assertIn(b"STOPPING=1", receiver.recv(4096))
            self.assertEqual(process.wait(timeout=5), 0)

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
