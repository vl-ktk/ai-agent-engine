"""Private, single-writer SQLite storage for the stage-5 task inbox.

No migrations of the existing engine SQLite database and no backup copies.
Every acknowledged mutation commits synchronously before it reaches RAM.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sqlite3
import stat


APPLICATION_ID = 0x4145494E  # "AEIN": separate from the existing engine DB.
SCHEMA_VERSION = 3  # v3 adds durable lifecycle and metadata-only audit.


class StorageUnavailable(RuntimeError):
    """Sanitized storage failure: do not expose DB paths or task prompts."""


class SQLiteTaskStore:
    """Exclusive local SQLite writer; parent directory 0700, database 0600."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).absolute()
        self._fd: int | None = None
        self._connection: sqlite3.Connection | None = None
        try:
            parent = os.lstat(self.path.parent)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise StorageUnavailable("database directory must be private 0700")
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            self._fd = os.open(self.path, flags, 0o600)
            actual = os.fstat(self._fd)
            if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.geteuid()
                    or stat.S_IMODE(actual.st_mode) != 0o600):
                raise StorageUnavailable("database file must be private 0600")
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StorageUnavailable("database already owned by another process") from exc
            # Every operation is serialized by TaskInbox._lock; the HTTP
            # server handles requests in different threads.
            self._connection = sqlite3.connect(str(self.path), timeout=5,
                                               check_same_thread=False)
            db = self._connection
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=5000")
            self._initialize_or_verify()
            # Never modify journal mode of a foreign/unrecognized database.
            db.execute("PRAGMA journal_mode=DELETE")
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise StorageUnavailable("cannot open private task database") from exc
        except Exception:
            self.close()
            raise

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageUnavailable("database is closed")
        return self._connection

    def _initialize_or_verify(self) -> None:
        db = self._db()
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if (app_id, version) == (0, 0):
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables:
                raise StorageUnavailable("unrecognized existing database")
            db.execute("BEGIN IMMEDIATE")
            with db:
                db.execute("""CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    receipt INTEGER UNIQUE NOT NULL,
                    owner_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    parent_task_id TEXT REFERENCES tasks(task_id),
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    submitted_priority INTEGER NOT NULL CHECK(submitted_priority BETWEEN 0 AND 100),
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100)
                )""")
                db.execute("""CREATE TABLE dependencies (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    prerequisite_id TEXT NOT NULL REFERENCES tasks(task_id),
                    PRIMARY KEY(task_id, prerequisite_id),
                    CHECK(task_id <> prerequisite_id)
                )""")
                self._create_claims_table(db)
                self._create_lifecycle_tables(db)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif app_id == APPLICATION_ID and version in (1, 2):
            # Migrate only our own private database. Explicit BEGIN makes DDL
            # and backfill a single rollback-safe transaction.
            if (db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                    or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise StorageUnavailable("cannot migrate damaged task database")
            db.execute("BEGIN IMMEDIATE")
            with db:
                if version == 1:
                    self._create_claims_table(db)
                self._create_lifecycle_tables(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif (app_id, version) != (APPLICATION_ID, SCHEMA_VERSION):
            raise StorageUnavailable("unexpected task database schema")
        try:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise StorageUnavailable("task database integrity check failed")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StorageUnavailable("task database references are inconsistent")
        except sqlite3.Error as exc:
            raise StorageUnavailable("task database integrity check failed") from exc

    @staticmethod
    def _create_claims_table(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE task_claims (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            worker_id TEXT NOT NULL,
            workspace_name TEXT NOT NULL,
            workspace_dir TEXT NOT NULL
        )""")

    @staticmethod
    def _create_lifecycle_tables(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE task_states (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            state TEXT NOT NULL CHECK(state IN
                ('queued','assigned','running','succeeded','failed','cancelled','timed_out'))
        )""")
        # Existing v1/v2 admissions and immutable claims determine their
        # factual state; no RUNNING or terminal progress is fabricated.
        db.execute("""INSERT INTO task_states (task_id,state)
            SELECT t.task_id, CASE WHEN c.task_id IS NULL THEN 'queued'
                ELSE 'assigned' END
            FROM tasks t LEFT JOIN task_claims c ON c.task_id=t.task_id""")
        db.execute("""CREATE TABLE task_transition_audit (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
            reason_code TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")

    def load(self) -> tuple[list[tuple], list[tuple[str, str]], list[tuple], list[tuple]]:
        """Read committed records/edges/claims/states; caller validates scope."""
        try:
            db = self._db()
            rows = db.execute("""SELECT task_id, receipt, owner_id, project_id,
                parent_task_id, title, prompt, submitted_priority, priority
                FROM tasks ORDER BY receipt""").fetchall()
            edges = db.execute("""SELECT task_id, prerequisite_id FROM dependencies
                ORDER BY task_id, prerequisite_id""").fetchall()
            claims = db.execute("""SELECT task_id, worker_id, workspace_name,
                workspace_dir FROM task_claims ORDER BY task_id""").fetchall()
            states = db.execute("""SELECT task_id, state FROM task_states
                ORDER BY task_id""").fetchall()
            return rows, edges, claims, states
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot load task database") from exc

    def insert_task(self, record) -> None:
        try:
            with self._db() as db:
                db.execute("""INSERT INTO tasks
                    (task_id,receipt,owner_id,project_id,parent_task_id,title,prompt,
                     submitted_priority,priority) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (record.task_id, record.receipt, record.owner_id,
                     record.project_id, record.parent_task_id,
                     record.submission.title, record.submission.prompt,
                     record.submission.priority, record.submission.priority))
                db.execute("INSERT INTO task_states (task_id,state) VALUES (?, 'queued')",
                           (record.task_id,))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist task") from exc

    def replace_dependencies(self, task_id: str, prerequisites: tuple[str, ...]) -> None:
        try:
            with self._db() as db:
                db.execute("DELETE FROM dependencies WHERE task_id=?", (task_id,))
                db.executemany("""INSERT INTO dependencies (task_id,prerequisite_id)
                    VALUES (?,?)""", ((task_id, dep) for dep in prerequisites))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist dependencies") from exc

    def update_priority(self, task_id: str, priority: int) -> None:
        try:
            with self._db() as db:
                cursor = db.execute("UPDATE tasks SET priority=? WHERE task_id=?",
                                    (priority, task_id))
                if cursor.rowcount != 1:
                    raise StorageUnavailable("task no longer persisted")
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist priority") from exc

    def insert_assignment(self, assignment) -> None:
        """Commit claim, status change and audit together, before HTTP ACK."""
        try:
            with self._db() as db:
                db.execute("""INSERT INTO task_claims
                    (task_id, worker_id, workspace_name, workspace_dir)
                    VALUES (?, ?, ?, ?)""",
                    (assignment.task_id, assignment.worker_id,
                     assignment.workspace_name, assignment.workspace_dir))
                cursor = db.execute("""UPDATE task_states SET state='assigned'
                    WHERE task_id=? AND state='queued'""", (assignment.task_id,))
                if cursor.rowcount != 1:
                    raise StorageUnavailable("task cannot be assigned from current state")
                self._append_audit(db, assignment.task_id, "queued", "assigned",
                                   assignment.worker_id, True, "claim")
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist assignment") from exc

    @staticmethod
    def _append_audit(db: sqlite3.Connection, task_id: str, previous: str,
                      requested: str, actor_id: str, accepted: bool,
                      reason_code: str) -> None:
        db.execute("""INSERT INTO task_transition_audit
            (task_id,from_state,to_state,actor_id,accepted,reason_code)
            VALUES (?,?,?,?,?,?)""",
            (task_id, previous, requested, actor_id, int(accepted), reason_code))

    def write_transition(self, task_id: str, previous: str, requested: str,
                         actor_id: str) -> None:
        """Compare-and-swap state and accepted audit in one transaction."""
        try:
            with self._db() as db:
                cursor = db.execute("""UPDATE task_states SET state=?
                    WHERE task_id=? AND state=?""", (requested, task_id, previous))
                if cursor.rowcount != 1:
                    raise StorageUnavailable("task state changed unexpectedly")
                self._append_audit(db, task_id, previous, requested,
                                   actor_id, True, "transition")
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist state transition") from exc

    def record_rejected_transition(self, task_id: str, previous: str,
                                   requested: str, actor_id: str,
                                   reason_code: str) -> None:
        """Rejected attempts are committed even when no state changes."""
        try:
            with self._db() as db:
                self._append_audit(db, task_id, previous, requested,
                                   actor_id, False, reason_code)
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist rejected transition") from exc

    def transition_history(self, task_id: str) -> list[tuple]:
        """Metadata only, ordered persistently by local monotonic event ID."""
        try:
            return self._db().execute("""SELECT event_id,task_id,from_state,
                to_state,actor_id,accepted,reason_code,occurred_at
                FROM task_transition_audit WHERE task_id=? ORDER BY event_id""",
                (task_id,)).fetchall()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read transition audit") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
