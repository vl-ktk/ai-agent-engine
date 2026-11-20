"""Offline 7.7 trusted failed-task bridge; no server or model calls."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
