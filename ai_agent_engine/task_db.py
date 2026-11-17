"""Versioned, fail-closed SQLite metadata store (stage 4.16).

The always-on controller is deliberately independent of this store. This
module provides the durable schema and minimal scoped CRUD; it does not run
agents, grant permissions, or infer which projects the owner has approved.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
from typing import Iterator


DEFAULT_DATABASE = Path("/var/lib/ai-agent-engine/projects/engine.sqlite3")
SCHEMA_VERSION = 2
TASK_STATUSES = frozenset({
    "CREATED", "QUEUED", "ASSIGNED", "RUNNING", "REVIEW", "RECOVERING",
    "WAITING", "COMPLETED", "FAILED", "CANCELLED",
})
PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})


class SchemaError(RuntimeError):
    """Unknown, corrupted or otherwise incompatible database schema."""


class UnsafeRollback(SchemaError):
    """Reversing the migration would discard existing data."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_private_file(path: Path, *, create: bool) -> None:
    """Reject symlinks, loose modes and directories before SQLite opens a path."""
    if not path.is_absolute():
        raise ValueError("database path must be absolute")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("database parent must be an existing real directory")
    if os.name == "posix" and parent.resolve(strict=True) != parent:
        raise ValueError("database parent traverses a symbolic link")
    if os.name == "posix" and parent.stat().st_mode & 0o077:
        raise PermissionError("database parent is accessible to other users")
    if os.name == "posix" and parent.stat().st_uid != os.geteuid():
        raise PermissionError("database parent owner differs from the process")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("database path must be a regular, unlinked file")
    if os.name == "posix" and info.st_mode & 0o077:
        raise PermissionError("database file permissions must be 0600 or stricter")
    if os.name == "posix" and info.st_uid != os.geteuid():
        raise PermissionError("database file owner differs from the process")


def connect_database(path: Path | str = DEFAULT_DATABASE, *, create: bool = False) -> sqlite3.Connection:
    """Open a private SQLite file; callers must explicitly call migrate()."""
    target = Path(path)
    _ensure_private_file(target, create=create)
    db = sqlite3.connect(str(target), timeout=5, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA journal_mode=WAL")
        return db
    except BaseException:
        db.close()
        raise


@contextmanager
def _write_transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def _version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _create_v1(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE projects (
        project_id TEXT PRIMARY KEY NOT NULL CHECK (length(project_id) BETWEEN 1 AND 128),
        registered_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY NOT NULL CHECK (length(task_id) BETWEEN 1 AND 128),
        project_id TEXT NOT NULL,
        parent_task_id TEXT,
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 1024),
        priority TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0','P1','P2','P3')),
        status TEXT NOT NULL DEFAULT 'CREATED' CHECK (status IN (
            'CREATED','QUEUED','ASSIGNED','RUNNING','REVIEW','RECOVERING',
            'WAITING','COMPLETED','FAILED','CANCELLED')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (project_id, task_id),
        FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT,
        FOREIGN KEY (project_id, parent_task_id)
            REFERENCES tasks(project_id, task_id) ON DELETE RESTRICT,
        CHECK (parent_task_id IS NULL OR parent_task_id <> task_id)
    )""")
    db.execute("""CREATE TABLE task_dependencies (
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        depends_on_id TEXT NOT NULL,
        dependency_kind TEXT NOT NULL DEFAULT 'REQUIRES',
        PRIMARY KEY (project_id, task_id, depends_on_id),
        FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
            ON DELETE CASCADE,
        FOREIGN KEY (project_id, depends_on_id) REFERENCES tasks(project_id, task_id)
            ON DELETE RESTRICT,
        CHECK (task_id <> depends_on_id)
    )""")
    db.execute("CREATE INDEX ix_tasks_project_status ON tasks(project_id, status, priority)")
    db.execute("INSERT INTO schema_migrations VALUES (1, 'task_metadata_v1', ?)", (_utc_now(),))
    db.execute("PRAGMA user_version=1")


def _create_v2(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE task_events (
        event_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
        created_at TEXT NOT NULL,
        FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
            ON DELETE RESTRICT
    )""")
    db.execute("CREATE INDEX ix_task_events_task ON task_events(project_id, task_id, created_at)")
    db.execute("INSERT INTO schema_migrations VALUES (2, 'task_events_v2', ?)", (_utc_now(),))
    db.execute("PRAGMA user_version=2")


def _rollback_v2(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM task_events LIMIT 1").fetchone():
        raise UnsafeRollback("task_events contains data; rollback refused")
    db.execute("DROP INDEX ix_task_events_task")
    db.execute("DROP TABLE task_events")
    db.execute("DELETE FROM schema_migrations WHERE version=2")
    db.execute("PRAGMA user_version=1")


def _validate_schema(db: sqlite3.Connection, version: int) -> None:
    if version == 0:
        # Never claim an unversioned database with unknown application tables.
        existing = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if existing:
            raise SchemaError("unversioned database contains tables")
        return
    names = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {"schema_migrations", "projects", "tasks", "task_dependencies"}
    if version >= 2:
        expected.add("task_events")
    if not expected.issubset(names):
        raise SchemaError("schema tables missing")
    if version == 1 and "task_events" in names:
        raise SchemaError("unexpected v2 table in v1 database")
    rows = db.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    migrations = [(r[0], r[1]) for r in rows]
    if migrations != [(1, "task_metadata_v1"), (2, "task_events_v2")][:version]:
        raise SchemaError("schema history does not match user_version")


def migrate(db: sqlite3.Connection, target_version: int = SCHEMA_VERSION) -> int:
    """Apply atomic migrations; only empty v2 events can safely roll back to v1."""
    if target_version not in (1, 2):
        raise ValueError("supported target schema versions: 1 and 2")
    with _write_transaction(db):
        version = _version(db)
        if version > SCHEMA_VERSION:
            raise SchemaError("database schema newer than this application")
        _validate_schema(db, version)
        if version == 0:
            _create_v1(db)
            version = 1
        if version == 1 and target_version == 2:
            _create_v2(db)
            version = 2
        elif version == 2 and target_version == 1:
            _rollback_v2(db)
            version = 1
        _validate_schema(db, version)
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SchemaError("foreign key integrity check failed")
    return version


def _require_scope(project_id: str, task_id: str | None = None) -> None:
    if not isinstance(project_id, str) or not 1 <= len(project_id) <= 128:
        raise ValueError("explicit project_id is required")
    if task_id is not None and (not isinstance(task_id, str) or not 1 <= len(task_id) <= 128):
        raise ValueError("explicit task_id is required")


def register_project(db: sqlite3.Connection, *, project_id: str) -> None:
    """Storage primitive only: caller must independently verify owner approval."""
    _require_scope(project_id)
    with _write_transaction(db):
        db.execute("INSERT INTO projects(project_id, registered_at) VALUES (?, ?)",
                   (project_id, _utc_now()))


def create_task(db: sqlite3.Connection, *, project_id: str, task_id: str,
                title: str, priority: str = "P2", parent_task_id: str | None = None) -> None:
    _require_scope(project_id, task_id)
    if not isinstance(title, str) or not 1 <= len(title) <= 1024:
        raise ValueError("title length is invalid")
    if priority not in PRIORITIES:
        raise ValueError("unknown priority")
    with _write_transaction(db):
        db.execute("""INSERT INTO tasks
            (task_id, project_id, parent_task_id, title, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, project_id, parent_task_id, title, priority, _utc_now(), _utc_now()))


def get_task(db: sqlite3.Connection, *, project_id: str, task_id: str) -> dict | None:
    _require_scope(project_id, task_id)
    row = db.execute("SELECT * FROM tasks WHERE project_id=? AND task_id=?",
                     (project_id, task_id)).fetchone()
    return dict(row) if row is not None else None


def update_task_status(db: sqlite3.Connection, *, project_id: str, task_id: str,
                       status: str, expected_revision: int) -> bool:
    _require_scope(project_id, task_id)
    if status not in TASK_STATUSES:
        raise ValueError("unknown status")
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("revision must be positive")
    with _write_transaction(db):
        cursor = db.execute("""UPDATE tasks
            SET status=?, revision=revision+1, updated_at=?
            WHERE project_id=? AND task_id=? AND revision=?""",
            (status, _utc_now(), project_id, task_id, expected_revision))
        return cursor.rowcount == 1


def delete_task(db: sqlite3.Connection, *, project_id: str, task_id: str) -> bool:
    _require_scope(project_id, task_id)
    with _write_transaction(db):
        cursor = db.execute("DELETE FROM tasks WHERE project_id=? AND task_id=?",
                            (project_id, task_id))
        return cursor.rowcount == 1


def smoke_test() -> None:
    """Only writes into a disposable directory, never the production database."""
    with TemporaryDirectory(prefix="ae-taskdb-smoke-") as temporary:
        path = Path(temporary) / "engine.sqlite3"
        with closing(connect_database(path, create=True)) as db:
            assert migrate(db) == 2
            register_project(db, project_id="smoke-only")
            create_task(db, project_id="smoke-only", task_id="smoke-task", title="Smoke task")
            assert get_task(db, project_id="smoke-only", task_id="smoke-task")["revision"] == 1
            assert update_task_status(db, project_id="smoke-only", task_id="smoke-task",
                                      status="COMPLETED", expected_revision=1)
            assert delete_task(db, project_id="smoke-only", task_id="smoke-task")
            assert migrate(db, 1) == 1
            assert migrate(db, 2) == 2
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private task database schema management")
    parser.add_argument("action", choices=("init", "check", "smoke"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    if args.action == "smoke":
        smoke_test()
        print("task DB disposable CRUD/rollback smoke: PASS")
        return 0
    with closing(connect_database(args.db, create=args.action == "init")) as db:
        if args.action == "init":
            version = migrate(db)
        else:
            version = _version(db)
            if version != SCHEMA_VERSION:
                raise SchemaError("database requires a migration or is incompatible")
            _validate_schema(db, version)
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SchemaError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SchemaError("foreign key check failed")
        print(f"task DB schema={version}, integrity=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
