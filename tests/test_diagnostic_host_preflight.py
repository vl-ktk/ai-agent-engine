"""Host preflight must never create, migrate or claim private runtime state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_agent_engine.diagnostic_host_preflight import inspect_preprovisioned_host
from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.project_leader import ProjectLeaderStore, draft_project
from ai_agent_engine.single_project_diagnostic_runner import SINGLE_SCOPE
from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_storage import SQLiteTaskStore


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private host preflight")
class DiagnosticHostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-77host-")
        self.base = Path(self.temp.name)
        self.project_root = self.base / "projects"
        self.project_root.mkdir(mode=0o700)
        self.workspace_root = self.base / "workspaces"
        self.workspace_root.mkdir(mode=0o700)
        self.source = self.workspace_root / "ai-agent-engine"
        self.source.mkdir(mode=0o700)
        self.socket_root = self.base / "socket"
        self.socket_root.mkdir(mode=0o700)
        self.task_db = self.base / "tasks.sqlite3"
        self.config = self.base / "runtime.json"

        subprocess.run(["git", "init", "-q", str(self.source)], check=True,
                       capture_output=True, timeout=5)
        (self.source / "README.md").write_text("host smoke fixture\n")
        subprocess.run(["git", "-C", str(self.source), "add", "README.md"],
                       check=True, capture_output=True, timeout=5)
        subprocess.run(["git", "-C", str(self.source),
                        "-c", "user.name=Offline Host", "-c",
                        "user.email=host@example.invalid", "commit", "-qm", "baseline"],
                       check=True, capture_output=True, timeout=5)
        self.head = subprocess.run(["git", "-C", str(self.source),
                                    "rev-parse", "HEAD"], capture_output=True,
                                   check=True, text=True, timeout=5).stdout.strip()

        self.registry = ProjectLeaderStore(self.project_root)
        proposal = draft_project(SINGLE_SCOPE, "ai-agent-engine", "One approved host project")
        self.registry.approve(SINGLE_SCOPE, proposal, OwnerDecision(
            SINGLE_SCOPE.owner_id, proposal.proposal_id, proposal.sha256, "button"))
        self.registry.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.task_db),
                               workspace_root=WorkspaceRoot(self.workspace_root))
        self.inbox.close()
        self.save_config()

    def tearDown(self):
        self.temp.cleanup()

    def save_config(self, **updates):
        payload = {"schema": 1, "owner_id": SINGLE_SCOPE.owner_id,
                   "project_ids": ["ai-agent-engine"],
                   "project_leader_root": str(self.project_root),
                   "workspace_root": str(self.workspace_root),
                   "task_db": str(self.task_db),
                   "socket_path": str(self.socket_root / "diagnostic.sock"),
                   "source_bindings": {"ai-agent-engine": str(self.source)}}
        payload.update(updates)
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.config.chmod(0o600)

    def test_existing_preapproved_host_is_read_only_candidate_ready(self):
        files = (self.task_db, self.project_root / "project-leaders.sqlite3",
                 self.project_root / "vl-ktk" / "ai-agent-engine" / "leader.sqlite3")
        before = {str(path): hashlib.sha256(path.read_bytes()).digest() for path in files}
        paths = {str(p) for p in self.base.rglob("*")}
        report = inspect_preprovisioned_host(self.config)
        self.assertTrue(report.candidate_ready, report)
        self.assertEqual(report.blockers, ())
        self.assertEqual(report.source_commit, self.head)
        self.assertEqual(len(report.checked), 5)
        self.assertEqual(before, {str(path): hashlib.sha256(path.read_bytes()).digest()
                                  for path in files})
        self.assertEqual(paths, {str(p) for p in self.base.rglob("*")})

    def test_operator_cli_prints_only_bounded_readiness_without_private_paths(self):
        run = subprocess.run([sys.executable, "-m",
                              "ai_agent_engine.diagnostic_host_preflight",
                              "--config", str(self.config)],
                             check=True, capture_output=True, text=True, timeout=10)
        data = json.loads(run.stdout)
        self.assertTrue(data["candidate_ready"])
        self.assertEqual(data["source_commit"], self.head)
        self.assertNotIn(str(self.base), run.stdout)
        self.config.chmod(0o644)
        denied = subprocess.run([sys.executable, "-m",
                                 "ai_agent_engine.diagnostic_host_preflight",
                                 "--config", str(self.config)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 2)
        self.assertEqual(json.loads(denied.stdout)["blockers"], ["private_config"])

    def test_no_private_config_or_wrong_owner_is_hold_without_creation(self):
        self.config.chmod(0o644)
        report = inspect_preprovisioned_host(self.config)
        self.assertEqual(report.blockers, ("private_config",))
        self.save_config(owner_id="unexpected-owner")
        self.assertEqual(inspect_preprovisioned_host(self.config).blockers,
                         ("single_project_identity",))

    def test_missing_task_database_never_created(self):
        self.task_db.unlink()
        report = inspect_preprovisioned_host(self.config)
        self.assertFalse(report.candidate_ready)
        self.assertIn("isolated_inactive_task_db", report.blockers)
        self.assertFalse(self.task_db.exists())

    def test_busy_task_owner_and_stale_assigned_work_both_hold(self):
        inbox = TaskInbox(store=SQLiteTaskStore(self.task_db),
                          workspace_root=WorkspaceRoot(self.workspace_root))
        try:
            active = inspect_preprovisioned_host(self.config)
            self.assertIn("isolated_inactive_task_db", active.blockers)
            task = inbox.accept(parse_submission(json.dumps({
                "project_id": "ai-agent-engine", "title": "Prior task",
                "prompt": "never execute in preflight"}).encode()), SINGLE_SCOPE)
            inbox.claim_task(task.task_id, "diagnostic-7-7", "ai-agent-engine",
                             SINGLE_SCOPE)
        finally:
            inbox.close()
        stale = inspect_preprovisioned_host(self.config)
        self.assertIn("isolated_inactive_task_db", stale.blockers)

    def test_foreign_tasks_or_pending_journal_fail_closed(self):
        inbox = TaskInbox(store=SQLiteTaskStore(self.task_db),
                          workspace_root=WorkspaceRoot(self.workspace_root))
        try:
            inbox.accept(parse_submission(json.dumps({
                "project_id": "foreign-project", "title": "Foreign task",
                "prompt": "do not inspect this payload"}).encode()),
                OwnerScope("foreign-owner", frozenset({"foreign-project"})))
        finally:
            inbox.close()
        self.assertIn("isolated_inactive_task_db",
                      inspect_preprovisioned_host(self.config).blockers)
        Path(str(self.task_db) + "-wal").write_bytes(b"sentinel")
        self.assertIn("isolated_inactive_task_db",
                      inspect_preprovisioned_host(self.config).blockers)

    def test_changed_database_mode_and_dirty_git_are_not_ready(self):
        self.task_db.chmod(0o644)
        self.assertIn("isolated_inactive_task_db",
                      inspect_preprovisioned_host(self.config).blockers)
        self.task_db.chmod(0o600)
        (self.source / "README.md").write_text("changed, not committed\n")
        report = inspect_preprovisioned_host(self.config)
        self.assertIn("clean_verified_git_source", report.blockers)

    def test_missing_owner_approval_is_not_fabricated(self):
        leader = self.project_root / "vl-ktk" / "ai-agent-engine" / "leader.sqlite3"
        leader.unlink()
        report = inspect_preprovisioned_host(self.config)
        self.assertIn("existing_project_approval", report.blockers)
        self.assertFalse(leader.exists())


if __name__ == "__main__":
    unittest.main()
