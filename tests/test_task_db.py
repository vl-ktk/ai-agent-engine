"""Stage 4.16: SQLite migrations, scoped CRUD, isolation and rollback."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from ai_agent_engine.task_db import (
    SCHEMA_VERSION, SchemaError, UnsafeRollback, connect_database, create_task,
    delete_task, get_task, main, migrate, register_project, smoke_test,
    update_task_status,
)


class TaskDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="ae-test-taskdb-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "engine.sqlite3"
        self.db = connect_database(self.path, create=True)
        self.addCleanup(self.db.close)

    def init(self):
        self.assertEqual(migrate(self.db), SCHEMA_VERSION)
        register_project(self.db, project_id="project-a")

    def test_new_db_and_idempotent_upgrade(self):
        self.assertEqual(migrate(self.db), 2)
        self.assertEqual(migrate(self.db), 2)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 2)
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_v1_to_v2_upgrade_keeps_task_data(self):
        self.assertEqual(migrate(self.db, 1), 1)
        register_project(self.db, project_id="project-a")
        create_task(self.db, project_id="project-a", task_id="task-1", title="Before")
        self.assertEqual(migrate(self.db, 2), 2)
        self.assertEqual(get_task(self.db, project_id="project-a", task_id="task-1")["title"], "Before")

    def test_rollback_then_upgrade_keeps_tasks(self):
        self.init()
        create_task(self.db, project_id="project-a", task_id="t", title="Keep")
        self.assertEqual(migrate(self.db, 1), 1)
        self.assertEqual(get_task(self.db, project_id="project-a", task_id="t")["title"], "Keep")
        self.assertEqual(migrate(self.db, 2), 2)
        self.assertEqual(migrate(self.db, 2), 2)

    def test_rollback_with_event_data_is_refused_atomically(self):
        self.init()
        create_task(self.db, project_id="project-a", task_id="t", title="Task")
        self.db.execute("INSERT INTO task_events VALUES (?, ?, ?, ?, ?)",
                        ("event", "project-a", "t", "CREATED", "2026-11-18T00:00:00Z"))
        with self.assertRaises(UnsafeRollback):
            migrate(self.db, 1)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM task_events").fetchone()[0], 1)

    def test_crud_revision_and_project_scope(self):
        self.init()
        register_project(self.db, project_id="project-b")
        create_task(self.db, project_id="project-a", task_id="t", title="A", priority="P0")
        self.assertIsNone(get_task(self.db, project_id="project-b", task_id="t"))
        self.assertFalse(update_task_status(self.db, project_id="project-b", task_id="t",
                                            status="RUNNING", expected_revision=1))
        self.assertFalse(delete_task(self.db, project_id="project-b", task_id="t"))
        self.assertTrue(update_task_status(self.db, project_id="project-a", task_id="t",
                                           status="RUNNING", expected_revision=1))
        self.assertFalse(update_task_status(self.db, project_id="project-a", task_id="t",
                                            status="COMPLETED", expected_revision=1))
        row = get_task(self.db, project_id="project-a", task_id="t")
        self.assertEqual((row["status"], row["revision"], row["priority"]), ("RUNNING", 2, "P0"))
        self.assertTrue(delete_task(self.db, project_id="project-a", task_id="t"))
        self.assertIsNone(get_task(self.db, project_id="project-a", task_id="t"))

    def test_cross_project_parent_and_dependency_rejected(self):
        self.init()
        register_project(self.db, project_id="project-b")
        create_task(self.db, project_id="project-a", task_id="a", title="A")
        create_task(self.db, project_id="project-b", task_id="b", title="B")
        with self.assertRaises(sqlite3.IntegrityError):
            create_task(self.db, project_id="project-b", task_id="bad",
                        title="Bad", parent_task_id="a")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO task_dependencies VALUES ('project-a', 'a', 'b', 'REQUIRES')")
        self.assertIsNone(get_task(self.db, project_id="project-b", task_id="bad"))

    def test_self_references_and_missing_project_rejected(self):
        self.init()
        with self.assertRaises(sqlite3.IntegrityError):
            create_task(self.db, project_id="unknown", task_id="x", title="None")
        with self.assertRaises(sqlite3.IntegrityError):
            create_task(self.db, project_id="project-a", task_id="x",
                        title="Self", parent_task_id="x")
        create_task(self.db, project_id="project-a", task_id="x", title="Exists")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO task_dependencies VALUES ('project-a', 'x', 'x', 'REQUIRES')")

    def test_parent_delete_restricted(self):
        self.init()
        create_task(self.db, project_id="project-a", task_id="parent", title="P")
        create_task(self.db, project_id="project-a", task_id="child",
                    title="C", parent_task_id="parent")
        with self.assertRaises(sqlite3.IntegrityError):
            delete_task(self.db, project_id="project-a", task_id="parent")

    def test_invalid_inputs(self):
        self.init()
        for kwargs in (
            dict(project_id="", task_id="t", title="X"),
            dict(project_id="project-a", task_id="t", title=""),
            dict(project_id="project-a", task_id="t", title="x", priority="BAD"),
        ):
            with self.assertRaises(ValueError):
                create_task(self.db, **kwargs)
        with self.assertRaises(ValueError):
            update_task_status(self.db, project_id="project-a", task_id="t",
                               status="BAD", expected_revision=1)
        with self.assertRaises(ValueError):
            update_task_status(self.db, project_id="project-a", task_id="t",
                               status="CREATED", expected_revision=True)

    def test_future_schema_refused(self):
        self.db.execute("PRAGMA user_version=99")
        with self.assertRaises(SchemaError):
            migrate(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_unknown_unversioned_schema_refused(self):
        self.db.execute("CREATE TABLE unknown_table(value TEXT)")
        with self.assertRaises(SchemaError):
            migrate(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_schema_history_drift_refused(self):
        self.init()
        self.db.execute("UPDATE schema_migrations SET name='tampered' WHERE version=2")
        with self.assertRaises(SchemaError):
            migrate(self.db)

    def test_existing_file_not_overwritten(self):
        other = self.path.parent / "existing.sqlite3"
        other.write_bytes(b"not a database")
        if os.name == "posix":
            other.chmod(0o600)
        with self.assertRaises(sqlite3.DatabaseError):
            with closing(connect_database(other, create=True)):
                pass
        self.assertEqual(other.read_bytes(), b"not a database")

    def test_cli_check_missing_file_not_created(self):
        self.assertEqual(main(["init", "--db", str(self.path)]), 0)
        self.assertEqual(main(["check", "--db", str(self.path)]), 0)
        missing = self.path.parent / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            main(["check", "--db", str(missing)])
        self.assertFalse(missing.exists())

    @unittest.skipUnless(os.name == "posix", "Unix-only mode enforcement")
    def test_file_mode_and_symlink_rejected(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.path.parent / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises((ValueError, OSError)):
            connect_database(link)
        self.path.chmod(0o644)
        with self.assertRaises(PermissionError):
            connect_database(self.path)

    @unittest.skipUnless(os.name == "posix", "Unix-only mode enforcement")
    def test_public_parent_rejected(self):
        public = self.path.parent / "public"
        public.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            connect_database(public / "unsafe.sqlite3", create=True)
        self.assertFalse((public / "unsafe.sqlite3").exists())

    def test_smoke_in_disposable_db(self):
        smoke_test()
        self.assertEqual(main(["smoke"]), 0)


if __name__ == "__main__":
    unittest.main()
