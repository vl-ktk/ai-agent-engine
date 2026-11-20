"""7.5: exact owner approval, private directories and project SQLite isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.project_leader import (
    InvalidProjectLeader, ProjectLeaderDenied, ProjectLeaderStore,
    ProjectLeaderUnavailable, draft_project, PROJECT_APPLICATION_ID,
    REGISTRY_APPLICATION_ID,
)
from ai_agent_engine.task_api import OwnerScope, ProjectForbidden
from ai_agent_engine.task_storage import APPLICATION_ID, SQLiteTaskStore


OWNER = OwnerScope("vl", frozenset({"ktk-social", "athl"}))
OTHER = OwnerScope("veta", frozenset({"ktk-social"}))
INVALID = OwnerScope("stranger", frozenset({"other-project"}))


def decision(proposal, channel="button"):
    return OwnerDecision(proposal.owner_id, proposal.proposal_id,
                         proposal.sha256, channel)


class ProjectLeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-75-")
        self.root = Path(self.temp.name) / "projects"
        self.root.mkdir(mode=0o700)
        self.store = ProjectLeaderStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def restart(self):
        self.store.close()
        self.store = ProjectLeaderStore(self.root)

    def approve(self, project="ktk-social", title="KTK Social", scope=OWNER):
        proposal = draft_project(scope, project, title)
        return proposal, self.store.approve(scope, proposal, decision(proposal))

    def test_draft_is_inert_and_never_creates_files(self):
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        self.assertEqual(self.store.get(OWNER, "ktk-social"), None)
        self.assertFalse((self.root / "vl").exists())
        self.assertIsNone(self.store._db().execute(
            "SELECT * FROM projects").fetchone())
        self.assertEqual(proposal.project_id, "ktk-social")

    def test_exact_approved_project_has_private_directory_and_sqlite(self):
        proposal, workspace = self.approve()
        self.assertEqual(workspace.owner_id, OWNER.owner_id)
        self.assertEqual(workspace.project_directory,
                         self.root / "vl" / "ktk-social")
        for path in (self.root / "vl", workspace.project_directory,
                     workspace.documents_directory):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.root / "project-leaders.sqlite3", workspace.database):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with sqlite3.connect(workspace.database) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0],
                             PROJECT_APPLICATION_ID)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT * FROM identity").fetchone(),
                             (1, "vl", "ktk-social", "KTK Social",
                              proposal.proposal_id, proposal.sha256, "button"))
            self.assertEqual(db.execute("SELECT count(*) FROM management_refs")
                             .fetchone()[0], 0)
        with sqlite3.connect(self.root / "project-leaders.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0],
                             REGISTRY_APPLICATION_ID)
            self.assertEqual(db.execute("SELECT state FROM projects").fetchone()[0],
                             "active")

    def test_no_source_checkout_or_extra_task_sqlite_is_created(self):
        _, workspace = self.approve()
        self.assertEqual({p.name for p in workspace.project_directory.iterdir()},
                         {"leader.sqlite3", "documents"})
        self.assertEqual({p.name for p in workspace.documents_directory.iterdir()},
                         set())
        self.assertFalse((self.root / "tasks.sqlite3").exists())

    def test_replay_same_approval_is_idempotent_across_restart(self):
        proposal, workspace = self.approve()
        ref = self.store.append_reference(OWNER, "ktk-social", kind="source_link",
                                          source_ref="github:repo@sha",
                                          checksum="a" * 64, verified=True)
        self.assertEqual(self.store.approve(OWNER, proposal, decision(proposal)),
                         workspace)
        self.restart()
        self.assertEqual(self.store.approve(OWNER, proposal, decision(proposal)),
                         workspace)
        self.assertEqual(self.store.references(OWNER, "ktk-social"), (ref,))
        self.assertEqual(self.store._db().execute(
            "SELECT count(*) FROM projects").fetchone()[0], 1)

    def test_simultaneous_approval_replays_do_not_duplicate_workspaces(self):
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.approve(
                OWNER, proposal, decision(proposal)), range(32)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.store._db().execute(
            "SELECT count(*) FROM projects").fetchone()[0], 1)

    def test_unauthorized_owner_project_and_forged_decision_are_denied(self):
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        for forged in (replace(decision(proposal), owner_id="veta"),
                       replace(decision(proposal), proposal_sha256="0" * 64),
                       replace(decision(proposal), channel="automatic"),
                       replace(proposal, title="Other project"),
                       replace(proposal, project_id="athl"),
                       replace(proposal, proposal_id="proposal_" + "b" * 32)):
            with self.subTest(forged=forged), self.assertRaises(
                    InvalidProjectLeader):
                self.store.approve(OWNER,
                    forged if isinstance(forged, type(proposal)) else proposal,
                    decision(proposal) if isinstance(forged, type(proposal)) else forged)
        with self.assertRaises(ProjectForbidden):
            draft_project(OWNER, "not-approved", "No")
        with self.assertRaises(InvalidProjectLeader):
            self.store.approve(INVALID, proposal, decision(proposal))
        with self.assertRaises(ProjectForbidden):
            self.store.approve(OwnerScope("vl",frozenset({"other-project"})),
                               proposal,decision(proposal))
        self.assertEqual(self.store.get(OWNER, "ktk-social"), None)

    def test_conflicting_approval_cannot_reinitialize_existing_project(self):
        first, original = self.approve()
        second = draft_project(OWNER, "ktk-social", "Different title")
        with self.assertRaises(ProjectLeaderDenied):
            self.store.approve(OWNER, second, decision(second))
        with self.assertRaises(ProjectLeaderDenied):
            self.store.approve(OWNER, first, decision(first, "message"))
        self.assertEqual(self.store.get(OWNER, "ktk-social"), original)

    def test_independent_projects_and_same_project_name_across_owners(self):
        _, a = self.approve()
        _, b = self.approve("athl", "ATHL")
        _, c = self.approve("ktk-social", "Veta KTK", OTHER)
        self.assertEqual(len({a.database, b.database, c.database}), 3)
        self.store.append_reference(OWNER, "ktk-social", kind="qa_report",
                                    source_ref="qa:one")
        self.store.append_reference(OWNER, "athl", kind="error", source_ref="issue:two")
        self.store.append_reference(OTHER, "ktk-social", kind="history",
                                    source_ref="veta:three")
        self.assertEqual([r.kind for r in self.store.references(OWNER,"ktk-social")],
                         ["qa_report"])
        self.assertEqual([r.kind for r in self.store.references(OWNER,"athl")],
                         ["error"])
        self.assertEqual([r.kind for r in self.store.references(OTHER,"ktk-social")],
                         ["history"])
        with self.assertRaises(ProjectForbidden):
            self.store.get(OTHER, "athl")
        with self.assertRaises(ProjectForbidden):
            self.store.references(INVALID, "ktk-social")

    def test_invalid_paths_titles_and_reference_fields_are_rejected(self):
        for name in ("../escape", ".", "..", "with/slash", "", "/tmp"):
            with self.subTest(name=name), self.assertRaises(
                    (InvalidProjectLeader, ProjectForbidden)):
                draft_project(OWNER, name, "Title")
        for title in ("", " heading", "heading\nsecret", "x" * 129):
            with self.subTest(title=title), self.assertRaises(InvalidProjectLeader):
                draft_project(OWNER, "ktk-social", title)
        self.approve()
        invalid = [dict(kind="unknown", source_ref="a"),
                   dict(kind="task", source_ref=""),
                   dict(kind="task", source_ref="secret\nleak"),
                   dict(kind="source_link", source_ref="a", verified=True),
                   dict(kind="source_link", source_ref="a", checksum="bad"),
                   dict(kind="task", source_ref="a", verified=1)]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(InvalidProjectLeader):
                self.store.append_reference(OWNER, "ktk-social", **item)
        self.assertEqual(self.store.references(OWNER, "ktk-social"), ())

    def test_incomplete_provisioning_is_durable_and_same_approval_repairs(self):
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        with patch.object(self.store, "_ensure_project", side_effect=OSError("crash")):
            with self.assertRaises(ProjectLeaderUnavailable):
                self.store.approve(OWNER, proposal, decision(proposal))
        self.assertEqual(self.store._db().execute(
            "SELECT state FROM projects").fetchone()[0], "provisioning")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.get(OWNER, "ktk-social")
        self.restart()
        self.assertIsNotNone(self.store.approve(OWNER, proposal, decision(proposal)))
        self.assertEqual(self.store._db().execute(
            "SELECT state FROM projects").fetchone()[0], "active")

    def test_sql_failure_leaves_recoverable_provisioning_not_false_success(self):
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        self.store._db().execute("""CREATE TRIGGER fail_activation
            BEFORE UPDATE ON projects BEGIN SELECT RAISE(ABORT,'test'); END""")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.approve(OWNER, proposal, decision(proposal))
        self.assertEqual(self.store._db().execute(
            "SELECT state FROM projects").fetchone()[0], "provisioning")
        self.store._db().execute("DROP TRIGGER fail_activation")
        self.assertIsNotNone(self.store.approve(OWNER, proposal, decision(proposal)))

    def test_active_missing_db_fails_closed_without_recreating(self):
        proposal, ws = self.approve()
        # Rename is recoverable, unlike deleting a user's database.
        ws.database.rename(ws.database.with_suffix(".saved"))
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.get(OWNER, "ktk-social")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.approve(OWNER, proposal, decision(proposal))
        self.assertFalse(ws.database.exists())

    def test_existing_database_with_wrong_identity_cannot_be_rebound(self):
        proposal, workspace = self.approve()
        with sqlite3.connect(workspace.database) as db:
            db.execute("UPDATE identity SET project_id='different'")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.get(OWNER, "ktk-social")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.approve(OWNER, proposal, decision(proposal))
        with sqlite3.connect(workspace.database) as db:
            self.assertEqual(db.execute("SELECT project_id FROM identity")
                             .fetchone()[0], "different")

    def test_unsafe_registry_permissions_block_restart(self):
        self.store.close()
        (self.root / "project-leaders.sqlite3").chmod(0o644)
        with self.assertRaises(ProjectLeaderUnavailable):
            ProjectLeaderStore(self.root)

    def test_symlink_project_database_is_not_followed(self):
        outside = Path(self.temp.name) / "outside.sqlite3"
        with sqlite3.connect(outside) as db:
            db.execute("CREATE TABLE private_data (token TEXT)")
        owner = self.root / "vl"
        owner.mkdir(mode=0o700)
        project = owner / "ktk-social"
        project.mkdir(mode=0o700)
        (project / "documents").mkdir(mode=0o700)
        (project / "leader.sqlite3").symlink_to(outside)
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.approve(OWNER, proposal, decision(proposal))
        with sqlite3.connect(outside) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM private_data")
                             .fetchone()[0], 0)

    def test_symlinks_and_unsafe_modes_are_denied(self):
        self.store.close()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(mode=0o700)
        (self.root / "vl").symlink_to(outside, target_is_directory=True)
        self.store = ProjectLeaderStore(self.root)
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.approve(OWNER, proposal, decision(proposal))
        self.store.close()
        (self.root / "vl").unlink()
        (self.root / "vl").mkdir(mode=0o755)
        self.store = ProjectLeaderStore(self.root)
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.approve(OWNER, proposal, decision(proposal))

    def test_foreign_or_damaged_project_database_is_not_overwritten(self):
        self.store.close()
        owner_dir = self.root / "vl"
        owner_dir.mkdir(mode=0o700)
        directory = owner_dir / "ktk-social"
        directory.mkdir(mode=0o700)
        (directory / "documents").mkdir(mode=0o700)
        dbpath = directory / "leader.sqlite3"
        with sqlite3.connect(dbpath) as db:
            db.execute("CREATE TABLE other_private_data (secret TEXT)")
            db.execute("INSERT INTO other_private_data VALUES ('not ours')")
        dbpath.chmod(0o600)
        self.store = ProjectLeaderStore(self.root)
        proposal = draft_project(OWNER, "ktk-social", "KTK Social")
        with self.assertRaises(ProjectLeaderUnavailable):
            self.store.approve(OWNER, proposal, decision(proposal))
        with sqlite3.connect(dbpath) as db:
            self.assertEqual(db.execute("SELECT secret FROM other_private_data")
                             .fetchone()[0], "not ours")

    def test_second_writer_is_rejected_until_first_closes(self):
        with self.assertRaises(ProjectLeaderUnavailable):
            ProjectLeaderStore(self.root)
        self.restart()
        self.assertIsNotNone(self.store._db())

    def test_task_sqlite_keeps_its_schema_and_records(self):
        task_path = Path(self.temp.name) / "tasks.sqlite3"
        store = SQLiteTaskStore(task_path)
        store.close()
        with sqlite3.connect(task_path) as db:
            original = (db.execute("PRAGMA application_id").fetchone()[0],
                        db.execute("PRAGMA user_version").fetchone()[0])
        self.approve()
        self.restart()
        with sqlite3.connect(task_path) as db:
            self.assertEqual(original,
                (db.execute("PRAGMA application_id").fetchone()[0],
                 db.execute("PRAGMA user_version").fetchone()[0]))
        self.assertEqual(original[0], APPLICATION_ID)


if __name__ == "__main__":
    unittest.main()
