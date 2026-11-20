"""Only disposable first-project approval tests; never contact Netcup."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from ai_agent_engine.diagnostic_host_preflight import inspect_preprovisioned_host
from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.project_leader import ProjectLeaderStore, PROJECT_SCHEMA_VERSION
from ai_agent_engine.task_api import OwnerScope


ROOT = Path(__file__).resolve().parents[1]
APPROVAL_PATH = ROOT / "ops/admin-scripts/approve_single_project_7_7.py"
PROVISION_PATH = ROOT / "ops/admin-scripts/provision_single_project_7_7.py"
VERIFY_PATH = ROOT / "ops/admin-scripts/verify_registered_project_7_7.py"


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


APPROVAL = load_script("approve_single_project_7_7", APPROVAL_PATH)
PROVISION = load_script("provision_single_project_7_7", PROVISION_PATH)
VERIFY = load_script("verify_registered_project_7_7", VERIFY_PATH)


class OwnerProjectRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-approve-77-")
        self.base = Path(self.temp.name)
        self.source = self.base / "original-source"
        self.source.mkdir(mode=0o700)
        self.git("init", "-q", str(self.source))
        (self.source / "README.md").write_text("private offline test\n")
        self.git("-C", str(self.source), "add", "README.md")
        self.git("-C", str(self.source), "-c", "user.name=Offline",
                 "-c", "user.email=offline@example.invalid", "commit",
                 "-qm", "initial")
        self.commit = self.git("-C", str(self.source), "rev-parse", "HEAD")
        self.root = self.base / "project-host"
        self.evidence = self.base / "evidence"
        self.evidence.mkdir(mode=0o700)
        PROVISION.provision_empty_project(
            self.root, source_url=str(self.source), commit=self.commit,
            uid=os.geteuid(), gid=os.getegid(), recovery=self.evidence)
        self.proposal = APPROVAL.load_pinned_proposal(ROOT)
        self.decision = OwnerDecision(
            self.proposal.owner_id, self.proposal.proposal_id,
            self.proposal.sha256, "button")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def git(*args):
        return subprocess.run(("git", *args), check=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()

    def register(self, proposal=None, decision=None):
        return APPROVAL.register_approved_project(
            self.root, proposal if proposal is not None else self.proposal,
            decision if decision is not None else self.decision,
            prepared_source_sha=self.commit)

    def test_pinned_proposal_is_exact_and_inert(self):
        self.assertEqual(self.proposal.owner_id, "vl-ktk")
        self.assertEqual(self.proposal.project_id, "ai-agent-engine")
        with ProjectLeaderStore(self.root / "projects") as store:
            self.assertIsNone(store.get(
                OwnerScope("vl-ktk", frozenset({"ai-agent-engine"})),
                "ai-agent-engine"))
        self.assertFalse((self.root / "projects" / "vl-ktk").exists())

    def test_approved_registration_creates_only_private_project_leader(self):
        report = self.register()
        self.assertEqual(report["status"], "APPROVED_INERT")
        self.assertFalse(report["service_activated"])
        self.assertTrue(report["approval_record_created"])
        db_path = self.root / "projects/vl-ktk/ai-agent-engine/leader.sqlite3"
        self.assertTrue(db_path.exists())
        self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
        for directory in (db_path.parent, db_path.parent / "documents"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        with sqlite3.connect(db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                             PROJECT_SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT proposal_id FROM identity").fetchone()[0],
                             self.proposal.proposal_id)
        with sqlite3.connect(self.root / "tasks.sqlite3") as task_db:
            self.assertEqual(task_db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        readiness = inspect_preprovisioned_host(self.root / "runtime.json")
        self.assertIn("existing_project_approval", readiness.checked)
        self.assertFalse(readiness.candidate_ready)
        self.assertIn("private_host_directories", readiness.blockers)  # no socket parent

    def test_same_approval_is_idempotent_and_does_not_reset_data(self):
        self.register()
        with ProjectLeaderStore(self.root / "projects") as store:
            store.append_reference(
                OwnerScope("vl-ktk", frozenset({"ai-agent-engine"})),
                "ai-agent-engine", kind="source_link", source_ref="test:kept")
        replay = self.register()
        self.assertFalse(replay["approval_record_created"])
        with sqlite3.connect(self.root / "projects/vl-ktk/ai-agent-engine/leader.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM management_refs").fetchone()[0], 1)

    def test_wrong_owner_decision_refused_without_project_write(self):
        for decision in (
            replace(self.decision, proposal_id="proposal_" + "0" * 32),
            replace(self.decision, channel="message"),
            replace(self.decision, proposal_sha256="0" * 64),
        ):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.register(decision=decision)
        self.assertFalse((self.root / "projects/vl-ktk").exists())

    def test_foreign_or_changed_proposal_is_denied(self):
        with self.assertRaises(ValueError):
            self.register(proposal=replace(self.proposal, title="Another"))
        with ProjectLeaderStore(self.root / "projects") as store:
            foreign = APPROVAL.load_pinned_proposal(ROOT)
            store._db().execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?,'active')",
                ("other", "unknown", "Other", "proposal_" + "f" * 32,
                 foreign.sha256, "button"))
            store._db().commit()
        with self.assertRaisesRegex(ValueError, "unexpected approvals"):
            self.register()
        self.assertFalse((self.root / "projects/vl-ktk").exists())

    def test_dirty_checkout_and_nonempty_tasks_refused(self):
        (self.root / "workspaces/ai-agent-engine/untracked").write_text("dirty")
        with self.assertRaises(Exception):
            self.register()
        (self.root / "workspaces/ai-agent-engine/untracked").unlink()
        with sqlite3.connect(self.root / "tasks.sqlite3") as db:
            db.execute(
                "INSERT INTO tasks "
                "(task_id,receipt,owner_id,project_id,title,prompt,submitted_priority,priority) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("other", 1, "other", "other", "other", "test", 50, 50))
        with self.assertRaises(Exception):
            self.register()
        self.assertFalse((self.root / "projects/vl-ktk").exists())

    def test_tampered_config_is_rejected_without_new_project(self):
        path = self.root / "runtime.json"
        doc = json.loads(path.read_text())
        doc["owner_id"] = "someone-else"
        path.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            self.register()
        self.assertFalse((self.root / "projects/vl-ktk").exists())

    def test_changed_proposal_bytes_are_denied(self):
        path = self.base / APPROVAL.PROPOSAL_FILE
        path.parent.mkdir(parents=True)
        path.write_bytes((ROOT / APPROVAL.PROPOSAL_FILE).read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "content changed"):
            APPROVAL.load_pinned_proposal(self.base)

    def test_independent_verification_reads_approved_project_without_writing(self):
        self.register()
        paths = (
            self.root / "runtime.json",
            self.root / "tasks.sqlite3",
            self.root / "projects/project-leaders.sqlite3",
            self.root / "projects/vl-ktk/ai-agent-engine/leader.sqlite3",
        )
        before = tuple(path.read_bytes() for path in paths)
        report = VERIFY.verify_registered_project(
            self.root, prepared_source_sha=self.commit)
        self.assertEqual(report["status"], "APPROVAL_VERIFIED_READ_ONLY")
        self.assertEqual(report["project_schema"], PROJECT_SCHEMA_VERSION)
        self.assertEqual(report["scope"], "vl-ktk/ai-agent-engine")
        self.assertFalse(report["service_activated"])
        self.assertEqual(tuple(path.read_bytes() for path in paths), before)

    def test_independent_verification_refuses_unapproved_and_wrong_source(self):
        with self.assertRaises(ValueError):
            VERIFY.verify_registered_project(self.root, prepared_source_sha=self.commit)
        self.register()
        with self.assertRaises(ValueError):
            VERIFY.verify_registered_project(self.root, prepared_source_sha="0" * 40)

    def test_independent_verification_refuses_extra_approved_project(self):
        self.register()
        with sqlite3.connect(self.root / "projects/project-leaders.sqlite3") as db:
            db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,'active')", (
                "other", "other", "Other", "proposal_" + "a" * 32,
                self.proposal.sha256, "button"))
        with self.assertRaisesRegex(ValueError, "extra project approvals"):
            VERIFY.verify_registered_project(self.root, prepared_source_sha=self.commit)

    def test_independent_verification_refuses_unexpected_task(self):
        self.register()
        with sqlite3.connect(self.root / "tasks.sqlite3") as db:
            db.execute(
                "INSERT INTO tasks "
                "(task_id,receipt,owner_id,project_id,title,prompt,submitted_priority,priority) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("task", 1, "vl-ktk", "ai-agent-engine", "check", "test", 50, 50))
        with self.assertRaisesRegex(ValueError, "diagnostic tasks unexpectedly exist"):
            VERIFY.verify_registered_project(self.root, prepared_source_sha=self.commit)


if __name__ == "__main__":
    unittest.main()
