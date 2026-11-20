"""Offline 7.7 trusted failed-task bridge; no server or model calls."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.diagnostic_intake_service import (
    IntakeUnavailable, MAX_EVENT_BYTES, _private_config,
    process_local_event, run_configured_service, serve_local_intake,
)
from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.project_diagnostic_adapter import TaskDiagnosticAdapter
from ai_agent_engine.project_diagnostics import (
    DiagnosticHold, FailureSignal, InvalidDiagnostic, ProjectDiagnosticsStore,
)
from ai_agent_engine.project_leader import ProjectLeaderStore, draft_project
from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


SCOPE = OwnerScope("owner77bridge", frozenset({"alpha"}))
OTHER = OwnerScope("other77bridge", frozenset({"alpha"}))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only trusted Git bridge")
class TaskDiagnosticAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-77bridge-")
        self.base = Path(self.temp.name)
        self.project_root = self.base / "projects"
        self.project_root.mkdir(mode=0o700)
        self.leaders = ProjectLeaderStore(self.project_root)
        proposal = draft_project(SCOPE, "alpha", "Approved offline project")
        self.leaders.approve(SCOPE, proposal, OwnerDecision(
            SCOPE.owner_id, proposal.proposal_id, proposal.sha256, "button"))

        self.workspace_root = self.base / "workspaces"
        self.workspace_root.mkdir(mode=0o700)
        self.repo = self.workspace_root / "slot-a"
        self.repo.mkdir(mode=0o700)
        self.git("init", "-q")
        (self.repo / "tracked.txt").write_text("baseline\n")
        self.git("add", "tracked.txt")
        self.git("-c", "user.name=Offline QA", "-c",
                 "user.email=qa@example.invalid", "commit", "-qm", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").strip()

        self.task_db = self.base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.task_db),
                               workspace_root=WorkspaceRoot(self.workspace_root))
        self.diagnostics = ProjectDiagnosticsStore(self.leaders,
                            source_bindings={(SCOPE.owner_id, "alpha"): self.repo})
        self.adapter = TaskDiagnosticAdapter(self.inbox, SCOPE, self.diagnostics)
        submission = parse_submission(json.dumps({
            "project_id": "alpha", "title": "Check failure",
            "prompt": "private task prompt is not diagnostic evidence",
        }).encode())
        self.task_id = self.inbox.accept(submission, SCOPE).task_id
        self.signal = FailureSignal(self.task_id, "test_failure", "AssertionError",
                                    "unit.test_case", "expected OK",
                                    "actual FAIL", "AssertionError in offline test")

    def tearDown(self):
        self.inbox.close()
        self.leaders.close()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              check=True, capture_output=True, text=True,
                              timeout=10).stdout

    def fail_task(self):
        self.inbox.claim_task(self.task_id, "worker-a", "slot-a", SCOPE)
        self.inbox.transition_state(self.task_id, TaskState.RUNNING, SCOPE,
                                    actor_id="worker-a")
        self.inbox.transition_state(self.task_id, TaskState.FAILED, SCOPE,
                                    actor_id="worker-a")

    def record(self, **kwargs):
        return self.adapter.record_failed_task(
            self.task_id, signal=kwargs.get("signal", self.signal),
            baseline_commit=kwargs.get("baseline", self.baseline))

    def test_proven_worker_failure_idempotent_and_durable(self):
        self.fail_task()
        first = self.record()
        self.assertEqual((first.occurrences, first.attempts, first.status),
                         (1, 0, "OPEN"))
        self.assertEqual(self.record(), first)
        rows = self.diagnostics.occurrences(SCOPE, "alpha", first.incident_id)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].delivery_key.startswith("taskfail_"))
        self.assertNotIn("private task prompt", repr(rows))
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.task_db),
                               workspace_root=WorkspaceRoot(self.workspace_root))
        self.adapter = TaskDiagnosticAdapter(self.inbox, SCOPE, self.diagnostics)
        self.assertEqual(self.record(), first)

    def test_unfailed_and_mismatched_task_are_held_without_evidence(self):
        with self.assertRaises(DiagnosticHold):
            self.record()
        self.fail_task()
        with self.assertRaises(DiagnosticHold):
            self.record(signal=FailureSignal("task_" + "a" * 32, "test_failure",
                "AssertionError", "unit.test_case", "expected OK", "actual FAIL",
                "safe evidence"))
        with self.assertRaises(InvalidDiagnostic):
            self.record(baseline="not-a-commit")
        self.assertEqual(self.record().occurrences, 1)

    def test_foreign_scope_and_wrong_project_source_are_held(self):
        self.fail_task()
        foreign = TaskDiagnosticAdapter(self.inbox, OTHER, self.diagnostics)
        with self.assertRaises(DiagnosticHold):
            foreign.record_failed_task(self.task_id, signal=self.signal,
                                       baseline_commit=self.baseline)
        other = self.base / "other-repo"
        other.mkdir(mode=0o700)
        misplaced = ProjectDiagnosticsStore(self.leaders,
            source_bindings={(SCOPE.owner_id, "alpha"): other})
        wrong = TaskDiagnosticAdapter(self.inbox, SCOPE, misplaced)
        with self.assertRaises(DiagnosticHold):
            wrong.record_failed_task(self.task_id, signal=self.signal,
                                     baseline_commit=self.baseline)
        self.assertEqual(self.record().occurrences, 1)

    def test_conflicting_retry_and_secret_text_fail_closed(self):
        self.fail_task()
        self.record()
        changed = FailureSignal(self.task_id, "test_failure", "AssertionError",
            "unit.test_case", "expected OK", "changed", "safe evidence")
        with self.assertRaises(DiagnosticHold):
            self.record(signal=changed)
        secret = FailureSignal(self.task_id, "test_failure", "AssertionError",
            "unit.test_case", "expected OK", "actual FAIL", "token=private")
        with self.assertRaises(DiagnosticHold):
            self.record(signal=secret)
        self.assertEqual(self.record().occurrences, 1)

    def test_recovery_requires_scoped_explicit_call(self):
        self.assertEqual(self.adapter.recover_project_unknown("alpha"), ())
        with self.assertRaises(DiagnosticHold):
            self.adapter.recover_project_unknown("other")

    def event(self, **overrides):
        item = {"schema": 1, "task_id": self.task_id,
                "baseline_commit": self.baseline,
                "failure": {"kind": self.signal.kind,
                            "error_code": self.signal.error_code,
                            "check_name": self.signal.check_name,
                            "expected": self.signal.expected,
                            "actual": self.signal.actual,
                            "excerpt": self.signal.excerpt}}
        item.update(overrides)
        return json.dumps(item, separators=(",", ":")).encode() + b"\n"

    def test_bounded_event_intake_fails_closed_and_is_idempotent(self):
        self.fail_task()
        first = process_local_event(self.adapter, self.event())
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(process_local_event(self.adapter, self.event()), first)
        for raw in (b"", b"{}\n", b"{}", b"x" * (MAX_EVENT_BYTES + 1),
                    b'{"schema":1,"schema":1}\n'):
            self.assertEqual(process_local_event(self.adapter, raw),
                             {"ok": False, "status": "HOLD"})
        invalid = self.event(failure={**json.loads(self.event())["failure"],
                                       "excerpt": "token=do-not-store"})
        self.assertEqual(process_local_event(self.adapter, invalid),
                         {"ok": False, "status": "HOLD"})

    def test_private_local_socket_single_uid_and_shutdown(self):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError:
            self.skipTest("AF_UNIX sockets unavailable in the test sandbox")
        else:
            probe.close()
        self.fail_task()
        path = self.base / "diagnostic.sock"
        stopped = Event()
        faults = []

        def run():
            try:
                serve_local_intake(self.adapter, path, stopped)
            except Exception as exc:
                faults.append(exc)

        thread = Thread(target=run, daemon=True)
        thread.start()
        for _ in range(100):
            if path.exists():
                break
            time.sleep(0.01)
        self.assertFalse(faults, faults)
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(path))
            client.sendall(self.event())
            with client.makefile("rb") as response:
                answer = json.loads(response.readline())
        self.assertEqual(answer["status"], "RECORDED")
        self.assertEqual(answer["ok"], True)
        with self.assertRaises(IntakeUnavailable):
            serve_local_intake(self.adapter, path, Event())
        stopped.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(faults)
        self.assertFalse(path.exists())

    def test_socket_refuses_unsafe_directory(self):
        self.fail_task()
        public = self.base / "unsafe"
        public.mkdir(mode=0o755)
        with self.assertRaises(IntakeUnavailable):
            serve_local_intake(self.adapter, public / "intake.sock", Event())

    def test_runtime_configuration_is_private_and_does_not_create_task_db(self):
        config = self.base / "runtime.json"
        payload = {
            "schema": 1, "owner_id": SCOPE.owner_id,
            "project_ids": ["alpha"], "project_leader_root": str(self.project_root),
            "workspace_root": str(self.workspace_root),
            "task_db": str(self.base / "missing-task-db.sqlite3"),
            "socket_path": str(self.base / "diagnostic.sock"),
            "source_bindings": {"alpha": str(self.repo)},
        }
        config.write_text(json.dumps(payload))
        config.chmod(0o600)
        self.assertEqual(_private_config(config), payload)
        with self.assertRaises(IntakeUnavailable):
            run_configured_service(config, Event())
        self.assertFalse(Path(payload["task_db"]).exists())
        config.chmod(0o644)
        with self.assertRaises(IntakeUnavailable):
            _private_config(config)
        config.chmod(0o600)
        payload["project_ids"] = ["alpha", "alpha"]
        config.write_text(json.dumps(payload))
        with self.assertRaises(IntakeUnavailable):
            _private_config(config)

    def test_runtime_loads_only_approved_project_and_prepared_task_db(self):
        config = self.base / "runtime.json"
        payload = {
            "schema": 1, "owner_id": SCOPE.owner_id,
            "project_ids": ["alpha"], "project_leader_root": str(self.project_root),
            "workspace_root": str(self.workspace_root),
            "task_db": str(self.task_db),
            "socket_path": str(self.base / "diagnostic.sock"),
            "source_bindings": {"alpha": str(self.repo)},
        }
        config.write_text(json.dumps(payload))
        config.chmod(0o600)
        self.inbox.close()
        self.leaders.close()
        called = []

        def observe(adapter, path, stopped):
            called.append((type(adapter).__name__, path))

        with patch("ai_agent_engine.diagnostic_intake_service.serve_local_intake",
                   side_effect=observe):
            run_configured_service(config, Event())
        self.assertEqual(called, [("TaskDiagnosticAdapter", Path(payload["socket_path"]))])


if __name__ == "__main__":
    unittest.main()
