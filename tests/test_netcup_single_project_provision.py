"""Disposable 7.7 Netcup provisioning tests. Never touch a real host."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from ai_agent_engine.project_leader import REGISTRY_APPLICATION_ID, SCHEMA_VERSION as REGISTRY_VERSION
from ai_agent_engine.diagnostic_host_preflight import inspect_preprovisioned_host
from ai_agent_engine.task_storage import APPLICATION_ID as TASK_APPLICATION_ID, SCHEMA_VERSION as TASK_VERSION


SCRIPT = (Path(__file__).resolve().parents[1] / "ops" / "admin-scripts"
          / "provision_single_project_7_7.py")
SPEC = importlib.util.spec_from_file_location("provision_single_project_7_7", SCRIPT)
PROVISION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROVISION)


class SingleProjectProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-provision-77-")
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir(mode=0o700)
        self.run_git("init", "-q", str(self.source))
        (self.source / "README.md").write_text("test source only\n", encoding="utf-8")
        self.run_git("-C", str(self.source), "add", "README.md")
        self.run_git("-C", str(self.source), "-c", "user.name=Offline",
                     "-c", "user.email=offline@example.invalid", "commit", "-qm", "original")
        self.sha = self.run_git("-C", str(self.source), "rev-parse", "HEAD")
        self.target = self.base / "private"
        self.recovery = self.base / "evidence"
        self.recovery.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def run_git(*args):
        return subprocess.run(("git", *args), check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    def provision(self, **updates):
        params = {"source_url": str(self.source), "commit": self.sha,
                  "uid": os.geteuid(), "gid": os.getegid(), "recovery": self.recovery}
        params.update(updates)
        return PROVISION.provision_empty_project(self.target, **params)

    def test_only_private_empty_schema_and_verified_git_are_created(self):
        report = self.provision()
        self.assertEqual(report["source_commit"], self.sha)
        self.assertFalse(report["owner_approval_created"])
        self.assertFalse(report["service_activated"])
        self.assertEqual(report["task_schema"], TASK_VERSION)
        self.assertEqual(report["registry_schema"], REGISTRY_VERSION)
        self.assertEqual(PROVISION._read_schema(self.target / "tasks.sqlite3"),
                         (TASK_APPLICATION_ID, TASK_VERSION))
        self.assertEqual(PROVISION._read_schema(self.target / "projects" / "project-leaders.sqlite3"),
                         (REGISTRY_APPLICATION_ID, REGISTRY_VERSION))
        with sqlite3.connect(self.target / "projects" / "project-leaders.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
        doc = json.loads((self.target / "runtime.json").read_text())
        self.assertEqual(doc["project_ids"], ["ai-agent-engine"])
        self.assertEqual(doc["owner_id"], "vl-ktk")
        self.assertEqual(self.run_git("-C", str(self.target / "workspaces" / "ai-agent-engine"),
                                      "rev-parse", "HEAD"), self.sha)
        self.assertEqual(self.run_git("-C", str(self.target / "workspaces" / "ai-agent-engine"),
                                      "status", "--porcelain"), "")
        self.assertFalse((self.target / "projects" / "vl-ktk").exists())
        self.assertFalse((self.base / "unit.service").exists())
        readiness = inspect_preprovisioned_host(self.target / "runtime.json")
        self.assertFalse(readiness.candidate_ready)
        self.assertIn("existing_project_approval", readiness.blockers)
        for f in (self.target / "tasks.sqlite3", self.target / "runtime.json",
                  self.target / "projects" / "project-leaders.sqlite3"):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        for d in (self.target, self.target / "projects", self.target / "workspaces",
                  self.target / "workspaces" / "ai-agent-engine"):
            self.assertEqual(d.stat().st_mode & 0o777, 0o700)

    def test_existing_state_is_not_overwritten(self):
        self.provision()
        before = (self.target / "runtime.json").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "already provisioned"):
            self.provision()
        self.assertEqual(before, (self.target / "runtime.json").read_bytes())
        self.assertFalse((self.recovery / "incomplete-single-project-provision").exists())

    def test_failed_git_fetch_preserves_recoverable_private_evidence(self):
        with self.assertRaises(Exception):
            self.provision(commit="0" * 40)
        self.assertFalse(self.target.exists())
        recovery = self.recovery / "incomplete-single-project-provision"
        self.assertTrue(recovery.exists())
        self.assertEqual(PROVISION._read_schema(recovery / "tasks.sqlite3"),
                         (TASK_APPLICATION_ID, TASK_VERSION))

    def test_source_symlink_is_refused_without_leaving_active_state(self):
        (self.source / "pointer").symlink_to("README.md")
        self.run_git("-C", str(self.source), "add", "pointer")
        self.run_git("-C", str(self.source), "-c", "user.name=Offline",
                     "-c", "user.email=offline@example.invalid", "commit", "-qm", "symbolic")
        self.sha = self.run_git("-C", str(self.source), "rev-parse", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.provision()
        self.assertFalse(self.target.exists())
        self.assertTrue((self.recovery / "incomplete-single-project-provision").exists())

    def test_invalid_sha_and_preexisting_symlink_do_not_create_anything(self):
        with self.assertRaisesRegex(RuntimeError, "unsafe or already"):
            self.provision(commit="not-a-commit")
        self.assertFalse(self.target.exists())
        self.target.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "unsafe or already"):
            self.provision()
        self.assertFalse((self.recovery / "incomplete-single-project-provision").exists())


if __name__ == "__main__":
    unittest.main()
