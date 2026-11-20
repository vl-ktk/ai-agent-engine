"""Inert managed runtime: one exclusive task owner, no grants or tasks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from ai_agent_engine.diagnostic_managed_runtime import (
    ManagedRuntimeHold, open_singleton, run_managed,
)
from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.project_leader import ProjectLeaderStore, draft_project
from ai_agent_engine.single_project_diagnostic_runner import SINGLE_SCOPE
from ai_agent_engine.task_api import TaskInbox
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only managed task owner")
class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-77managed-")
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir(mode=0o700)
        with ProjectLeaderStore(self.projects) as leaders:
            proposal = draft_project(SINGLE_SCOPE, "ai-agent-engine", "Managed runtime offline")
            leaders.approve(SINGLE_SCOPE, proposal, OwnerDecision(
                SINGLE_SCOPE.owner_id, proposal.proposal_id, proposal.sha256, "button"))
        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        self.source = self.workspaces / "ai-agent-engine"
        self.source.mkdir(mode=0o700)
        self.git("init", "-q")
        (self.source / "README.md").write_text("verified source\n")
        self.git("add", "README.md")
        self.git("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid",
                 "commit", "-qm", "baseline")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.socket_dir = self.root / "socket"
        self.socket_dir.mkdir(mode=0o700)
        self.db = self.root / "tasks.sqlite3"
        TaskInbox(store=SQLiteTaskStore(self.db),
                  workspace_root=WorkspaceRoot(self.workspaces)).close()
        self.config = self.root / "runtime.json"
        self.payload = {
            "schema": 1, "owner_id": "vl-ktk", "project_ids": ["ai-agent-engine"],
            "project_leader_root": str(self.projects),
            "workspace_root": str(self.workspaces), "task_db": str(self.db),
            "socket_path": str(self.socket_dir / "intake.sock"),
            "source_bindings": {"ai-agent-engine": str(self.source)},
        }
        self.save()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.source), *args], check=True,
                              capture_output=True, text=True, timeout=5).stdout

    def save(self):
        self.config.write_text(json.dumps(self.payload), encoding="utf-8")
        self.config.chmod(0o600)

    def test_approved_one_process_holds_exclusive_db_lock_and_no_grants(self):
        with open_singleton(self.config, expected_project_source=self.sha) as (runner, socket):
            self.assertEqual(socket, self.socket_dir / "intake.sock")
            self.assertTrue(runner.inbox.durable)
            self.assertIsNone(runner.run_next())  # no admitted tasks
            self.assertEqual(runner.gate._grants, {})
            self.assertFalse(socket.exists())  # not bound until actual service call
            with self.assertRaises(StorageUnavailable):
                SQLiteTaskStore(self.db)
        # Graceful close releases the real Linux cross-process flock.
        released = SQLiteTaskStore(self.db)
        released.close()

    def test_source_pin_is_fail_closed_before_opening_writable_storage(self):
        wrong_sha = "0" * 40
        with self.assertRaisesRegex(ManagedRuntimeHold, "approved_project_source_changed"):
            with open_singleton(self.config, expected_project_source=wrong_sha):
                self.fail("must not launch")
        with self.assertRaisesRegex(ManagedRuntimeHold, "invalid_project_source_pin"):
            with open_singleton(self.config, expected_project_source="not-a-sha"):
                self.fail("must not launch")
        with open_singleton(self.config, expected_project_source=self.sha):
            pass

    def test_unapproved_or_extra_project_and_unsafe_private_config_hold(self):
        self.payload["project_ids"] = ["ai-agent-engine", "another-project"]
        self.payload["source_bindings"]["another-project"] = str(self.source)
        self.save()
        with self.assertRaisesRegex(ManagedRuntimeHold, "private_host_not_ready"):
            with open_singleton(self.config, expected_project_source=self.sha):
                self.fail("must not launch")
        self.payload["project_ids"] = ["ai-agent-engine"]
        del self.payload["source_bindings"]["another-project"]
        self.save()
        self.config.chmod(0o644)
        with self.assertRaisesRegex(ManagedRuntimeHold, "private_host_not_ready"):
            with open_singleton(self.config, expected_project_source=self.sha):
                self.fail("must not launch")

    def test_runner_is_inert_and_receives_existing_adapter(self):
        def observe(adapter, path, stopped):
            self.assertEqual(path, self.socket_dir / "intake.sock")
            self.assertTrue(adapter._inbox.durable)
            self.assertFalse(stopped.is_set())
            with self.assertRaises(StorageUnavailable):
                SQLiteTaskStore(self.db)
        with patch("ai_agent_engine.diagnostic_managed_runtime.serve_local_intake", observe):
            run_managed(self.config, Event(), expected_project_source=self.sha)

    def test_foreign_socket_path_and_live_task_owner_hold(self):
        self.payload["socket_path"] = str(self.root / "outside" / "intake.sock")
        self.save()
        with self.assertRaisesRegex(ManagedRuntimeHold, "private_host_not_ready"):
            with open_singleton(self.config, expected_project_source=self.sha):
                self.fail("must not launch")
        self.payload["socket_path"] = str(self.socket_dir / "intake.sock")
        self.save()
        other = SQLiteTaskStore(self.db)
        try:
            with self.assertRaisesRegex(ManagedRuntimeHold, "private_host_not_ready"):
                with open_singleton(self.config, expected_project_source=self.sha):
                    self.fail("must not launch")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
