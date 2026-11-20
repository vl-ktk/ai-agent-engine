"""Linux offline one-project runner: real child, durable grants and failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.project_leader import ProjectLeaderStore, draft_project
from ai_agent_engine.project_diagnostics import DiagnosticHold
from ai_agent_engine.single_project_diagnostic_runner import (
    RunnerHold, SINGLE_SCOPE, WORKER_ID, SingleProjectDiagnosticRunner,
    _isolated_worker_env,
)
from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_permissions import (
    ActionGrant, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only isolated processes")
class SingleProjectDiagnosticRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-77runner-")
        self.base = Path(self.temp.name)
        self.project_root = self.base / "projects"
        self.project_root.mkdir(mode=0o700)
        self.leaders = ProjectLeaderStore(self.project_root)
        proposal = draft_project(SINGLE_SCOPE, "ai-agent-engine", "Only approved project")
        self.leaders.approve(SINGLE_SCOPE, proposal, OwnerDecision(
            SINGLE_SCOPE.owner_id, proposal.proposal_id, proposal.sha256, "button"))

        self.workspace_root = self.base / "workspaces"
        self.workspace_root.mkdir(mode=0o700)
        self.source = self.workspace_root / "slot-a"
        self.source.mkdir(mode=0o700)
        self.git("init", "-q")
        (self.source / "code.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "code.py")
        self.git("-c", "user.name=Offline Runner", "-c",
                 "user.email=qa@example.invalid", "commit", "-qm", "baseline")

        self.task_db = self.base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.task_db),
                               workspace_root=WorkspaceRoot(self.workspace_root))
        self.config = self.base / "runtime.json"
        self.config.write_text(json.dumps({
            "schema": 1, "owner_id": SINGLE_SCOPE.owner_id,
            "project_ids": ["ai-agent-engine"],
            "project_leader_root": str(self.project_root),
            "workspace_root": str(self.workspace_root),
            "task_db": str(self.task_db),
            "socket_path": str(self.base / "diagnostic.sock"),
            "source_bindings": {"ai-agent-engine": str(self.source)},
        }), encoding="utf-8")
        self.config.chmod(0o600)
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.leaders.close()
        self.temp.cleanup()

    def git(self, *argv):
        return subprocess.run(["git", "-C", str(self.source), *argv], check=True,
                              text=True, capture_output=True, timeout=5).stdout

    def admit(self, *, priority=0):
        submission = parse_submission(json.dumps({
            "project_id": "ai-agent-engine", "title": "Trusted offline check",
            "prompt": "not an executable command", "priority": priority,
        }).encode())
        return self.inbox.accept(submission, SINGLE_SCOPE).task_id

    def runner(self, *ids):
        grants = tuple(ActionGrant(tid, WORKER_ID, ProtectedAction.EXECUTE_TOOL)
                       for tid in ids)
        runner = SingleProjectDiagnosticRunner(
            inbox=self.inbox, scope=SINGLE_SCOPE, leaders=self.leaders,
            config=self.config, gate=TaskPermissionGate(self.inbox, grants=grants))
        self.runners.append(runner)
        return runner

    def test_one_scoped_queue_task_real_git_child_success_no_incident(self):
        tid = self.admit()
        runner = self.runner(tid)
        outcome = runner.run_next()
        self.assertEqual((outcome.task_id, outcome.state, outcome.incident_id),
                         (tid, TaskState.SUCCEEDED, None))
        self.assertEqual(self.inbox.state(tid, SINGLE_SCOPE), TaskState.SUCCEEDED)
        self.assertEqual(runner.delivery.snapshot(tid, SINGLE_SCOPE).status,
                         "completed")
        self.assertEqual(len(runner.gate.audit_history(tid, SINGLE_SCOPE)), 1)
        self.assertIsNone(runner.run_next())
        self.assertFalse((self.base / "diagnostic.sock").exists())

    def test_real_bad_git_diff_records_one_verified_incident(self):
        tid = self.admit()
        (self.source / "code.py").write_text("value = 1  \n", encoding="utf-8")
        runner = self.runner(tid)
        outcome = runner.run_next()
        self.assertEqual(outcome.state, TaskState.FAILED)
        self.assertTrue(outcome.incident_id.startswith("inc_"))
        self.assertEqual(runner.delivery.snapshot(tid, SINGLE_SCOPE).status,
                         "exhausted")
        recorded = runner.adapter._diagnostics.occurrences(
            SINGLE_SCOPE, "ai-agent-engine", outcome.incident_id)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].check_name, "git_diff_check")
        self.assertNotIn("not an executable command", repr(recorded))
        self.assertIsNone(runner.run_next())

    def test_changed_source_after_worker_exit_is_held_without_incident(self):
        tid = self.admit()
        runner = self.runner(tid)
        from ai_agent_engine.project_diagnostics import capture_git_source
        observations = 0

        def change_between_checks(path, baseline):
            nonlocal observations
            observations += 1
            if observations == 2:
                (self.source / "code.py").write_text("value = 2\n", encoding="utf-8")
            return capture_git_source(path, baseline)

        with patch("ai_agent_engine.single_project_diagnostic_runner.capture_git_source",
                   side_effect=change_between_checks):
            with self.assertRaisesRegex(RunnerHold, "execution_requires_owner_review"):
                runner.run_next()
        self.assertEqual(self.inbox.state(tid, SINGLE_SCOPE), TaskState.CANCELLED)
        self.assertEqual(observations, 2)

    def test_diagnostic_write_failure_keeps_real_failed_task_durable(self):
        tid = self.admit()
        (self.source / "code.py").write_text("value = 1  \n", encoding="utf-8")
        runner = self.runner(tid)
        with patch.object(runner.adapter._diagnostics, "record_failure",
                          side_effect=DiagnosticHold("offline_write_hold")):
            with self.assertRaisesRegex(RunnerHold, "execution_requires_owner_review"):
                runner.run_next()
        self.assertEqual(self.inbox.state(tid, SINGLE_SCOPE), TaskState.FAILED)
        self.assertEqual(runner.delivery.snapshot(tid, SINGLE_SCOPE).status,
                         "exhausted")

    def test_no_grant_refuses_before_child_exec_and_no_fake_failure(self):
        tid = self.admit()
        runner = self.runner()  # explicit no grants
        with self.assertRaisesRegex(RunnerHold, "execution_requires_owner_review"):
            runner.run_next()
        self.assertEqual(self.inbox.state(tid, SINGLE_SCOPE), TaskState.CANCELLED)
        audit = runner.gate.audit_history(tid, SINGLE_SCOPE)
        self.assertEqual(len(audit), 1)
        self.assertFalse(audit[0].allowed)
        self.assertEqual(len([e for e in self.inbox.transition_history(tid, SINGLE_SCOPE)
                              if e.to_state is TaskState.FAILED and e.accepted]), 0)

    def test_any_preexisting_assigned_work_holds_new_queued_task(self):
        first = self.admit()
        second = self.admit()
        self.inbox.claim_task(first, WORKER_ID, "slot-a", SINGLE_SCOPE)
        runner = self.runner(second)
        with self.assertRaisesRegex(RunnerHold, "unfinished_task_requires_owner_review"):
            runner.run_next()
        self.assertIs(self.inbox.state(second, SINGLE_SCOPE), TaskState.QUEUED)

    def test_only_exact_project_and_existing_approved_registry(self):
        tid = self.admit()
        with self.assertRaisesRegex(RunnerHold, "single_approved_project"):
            SingleProjectDiagnosticRunner(inbox=self.inbox,
                scope=OwnerScope("other", frozenset({"ai-agent-engine"})),
                leaders=self.leaders, config=self.config,
                gate=TaskPermissionGate(self.inbox))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-fixture-do-not-export"}):
            self.assertNotIn("OPENAI_API_KEY", _isolated_worker_env())
        runner = self.runner(tid)
        self.assertIs(runner.inbox, self.inbox)
        with self.assertRaisesRegex(RunnerHold, "one_runner_per_task_inbox_required"):
            self.runner(tid)
        runner._run_lock.acquire()
        try:
            with self.assertRaisesRegex(RunnerHold, "runner_already_active"):
                runner.run_next()
        finally:
            runner._run_lock.release()
        self.assertIs(self.inbox.state(tid, SINGLE_SCOPE), TaskState.QUEUED)

    def test_priority_selects_only_one_approved_task(self):
        lower = self.admit(priority=0)
        higher = self.admit(priority=9)
        runner = self.runner(lower, higher)
        self.assertEqual(runner.run_next().task_id, higher)
        self.assertIs(self.inbox.state(lower, SINGLE_SCOPE), TaskState.QUEUED)
