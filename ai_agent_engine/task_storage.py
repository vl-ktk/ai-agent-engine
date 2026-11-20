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
SCHEMA_VERSION = 16  # v16: durable owner-appointed global direction leads.


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
                self._create_delivery_tables(db)
                self._create_permission_audit_table(db)
                self._create_provider_audit_table(db)
                self._create_process_table(db)
                self._create_artifact_tables(db)
                self._create_source_version_table(db)
                self._create_owner_approval_table(db)
                self._create_plan_table(db)
                self._create_specialist_tables(db)
                self._create_transfer_tables(db)
                self._create_crisis_tables(db)
                self._create_global_lead_tables(db)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif app_id == APPLICATION_ID and 1 <= version <= 15:
            # Migrate only our own private database. Explicit BEGIN makes DDL
            # and backfill a single rollback-safe transaction.
            if (db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                    or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise StorageUnavailable("cannot migrate damaged task database")
            db.execute("BEGIN IMMEDIATE")
            with db:
                if version == 1:
                    self._create_claims_table(db)
                if version in (1, 2):
                    self._create_lifecycle_tables(db)
                if version in (1, 2, 3):
                    self._create_delivery_tables(db)
                if version <= 4:
                    self._create_permission_audit_table(db)
                if version <= 5:
                    self._create_provider_audit_table(db)
                elif version == 6:
                    # SQLite cannot ALTER a CHECK constraint. Rebuild only
                    # our private audit table, retaining its event IDs and
                    # timestamps. All DDL and copying roll back together.
                    db.execute("ALTER TABLE task_provider_audit "
                               "RENAME TO task_provider_audit_v6")
                    self._create_provider_audit_table(db)
                    db.execute("""INSERT INTO task_provider_audit
                        (event_id,task_id,provider,model_id,outcome,occurred_at)
                        SELECT event_id,task_id,provider,model_id,outcome,occurred_at
                        FROM task_provider_audit_v6""")
                    db.execute("DROP TABLE task_provider_audit_v6")
                self._create_process_table(db)
                self._create_artifact_tables(db)
                self._create_source_version_table(db)
                self._create_owner_approval_table(db)
                self._create_plan_table(db)
                if version <= 14:
                    # v14 adds transfer_reassigned; v15 adds crisis_fenced.
                    # Older permission audit CHECKs cannot accept both.
                    db.execute("ALTER TABLE task_permission_audit "
                               "RENAME TO task_permission_audit_v12")
                    self._create_permission_audit_table(db)
                    db.execute("""INSERT INTO task_permission_audit
                        (event_id,task_id,action,allowed,reason_code,principal_kind,occurred_at)
                        SELECT event_id,task_id,action,allowed,reason_code,principal_kind,occurred_at
                        FROM task_permission_audit_v12""")
                    db.execute("DROP TABLE task_permission_audit_v12")
                self._create_specialist_tables(db)
                self._create_transfer_tables(db)
                self._create_crisis_tables(db)
                self._create_global_lead_tables(db)
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

    @staticmethod
    def _create_delivery_tables(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE task_admission_keys (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
            PRIMARY KEY(owner_id,project_id,key_hash)
        )""")
        db.execute("""CREATE TABLE task_deliveries (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 5),
            attempts_used INTEGER NOT NULL CHECK(attempts_used BETWEEN 1 AND 5),
            attempt_token TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('active','retryable','completed','exhausted')),
            retry_at REAL NOT NULL DEFAULT 0,
            result TEXT CHECK(result IN ('success','failure') OR result IS NULL),
            CHECK(attempts_used <= max_attempts)
        )""")

    @staticmethod
    def _create_permission_audit_table(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE task_permission_audit (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            action TEXT NOT NULL CHECK(action IN
                ('read_secret','write_workspace','execute_tool',
                 'external_request','delete_artifact')),
            allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
            reason_code TEXT NOT NULL CHECK(reason_code IN
                ('granted','grant_missing','grant_expired','grant_revoked',
                 'emergency_hold','role_policy_denied',
                 'worker_mismatch','task_not_running','transfer_reassigned',
                 'crisis_fenced')),
            principal_kind TEXT NOT NULL CHECK(principal_kind IN
                ('assigned_worker','other_worker')),
            occurred_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")

    @staticmethod
    def _create_provider_audit_table(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE task_provider_audit (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            provider TEXT NOT NULL CHECK(provider IN ('gpt','claude')),
            model_id TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN
                ('completed','quota','auth','unsupported','unavailable',
                 'worker_failure','invalid_result','timed_out','cancelled')),
            occurred_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")

    @staticmethod
    def _create_process_table(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS task_process_runs (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            worker_id TEXT NOT NULL,
            pid INTEGER NOT NULL CHECK(pid > 1),
            pgid INTEGER NOT NULL CHECK(pgid > 1),
            sid INTEGER NOT NULL CHECK(sid > 1),
            start_ticks INTEGER NOT NULL CHECK(start_ticks > 0),
            boot_id TEXT NOT NULL,
            launch_kind TEXT NOT NULL CHECK(launch_kind IN ('direct','gated')),
            status TEXT NOT NULL CHECK(status IN ('active','stopped')),
            recorded_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            stopped_at TEXT
        )""")


    @staticmethod
    def _create_artifact_tables(db: sqlite3.Connection) -> None:
        """Private v9 bundle: one verified report and immutable byte payloads.

        The artifact bytes are stored *inside* this private SQLite so report
        admission and all files can commit or roll back together.
        """
        db.execute("""CREATE TABLE IF NOT EXISTS task_artifact_reports (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            response_json BLOB NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_artifact_blobs (
            task_id TEXT NOT NULL REFERENCES task_artifact_reports(task_id),
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 2097152),
            media_type TEXT NOT NULL,
            body BLOB NOT NULL,
            PRIMARY KEY(task_id,path),
            CHECK(length(body) = size_bytes)
        )""")

    def write_artifact_bundle(self, task_id: str, response_json: bytes,
                              blobs: tuple[tuple[str, str, int, str, bytes], ...]) -> None:
        """Store a verified successful report and byte payloads atomically.

        A second submission is rejected by the task_id primary key. Provider
        grant/checksum verification belongs to the trusted caller.
        """
        try:
            with self._db() as db:
                row = db.execute("""SELECT s.state,d.status,d.result
                    FROM task_states s LEFT JOIN task_deliveries d
                      ON d.task_id=s.task_id
                    WHERE s.task_id=?""", (task_id,)).fetchone()
                if row != ("succeeded", "completed", "success"):
                    raise StorageUnavailable("task has no completed delivery")
                db.execute("""INSERT INTO task_artifact_reports
                    (task_id,response_json) VALUES (?,?)""",
                    (task_id, response_json))
                db.executemany("""INSERT INTO task_artifact_blobs
                    (task_id,path,sha256,size_bytes,media_type,body)
                    VALUES (?,?,?,?,?,?)""",
                    ((task_id, path, digest, size, media, body)
                     for path, digest, size, media, body in blobs))
        except sqlite3.IntegrityError as exc:
            raise StorageUnavailable("artifact bundle already exists or is invalid") from exc
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot save verified artifact bundle") from exc

    def read_artifact_bundle(self, task_id: str) -> tuple[bytes, tuple[tuple, ...]] | None:
        """Read private bytes for an already scope-checked core caller."""
        try:
            db = self._db()
            report = db.execute("""SELECT response_json FROM task_artifact_reports
                WHERE task_id=?""", (task_id,)).fetchone()
            if report is None:
                return None
            # Guard against accidental large/corrupt private DB content.
            meta = db.execute("""SELECT COUNT(*),COALESCE(SUM(length(body)),0)
                FROM task_artifact_blobs WHERE task_id=?""", (task_id,)).fetchone()
            if meta[0] > 16 or meta[1] > 8_388_608 or len(report[0]) > 131_072:
                raise StorageUnavailable("stored artifact bundle exceeds limits")
            rows = db.execute("""SELECT path,sha256,size_bytes,media_type,body
                FROM task_artifact_blobs WHERE task_id=? ORDER BY path""",
                (task_id,)).fetchall()
            return bytes(report[0]), tuple(rows)
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read stored artifact bundle") from exc

    @staticmethod
    def _create_source_version_table(db: sqlite3.Connection) -> None:
        """Stage 6.13: task-local immutable Git head + patch digest.

        A version receipt is not a Git commit or an automatic git push.
        """
        db.execute("""CREATE TABLE IF NOT EXISTS task_source_versions (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            worker_id TEXT NOT NULL,
            baseline_commit TEXT NOT NULL,
            result_commit TEXT NOT NULL,
            patch_sha256 TEXT,
            recorded_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            CHECK(length(baseline_commit) IN (40,64)),
            CHECK(length(result_commit) IN (40,64)),
            CHECK(patch_sha256 IS NULL OR length(patch_sha256) = 64)
        )""")

    def write_source_version(self, task_id: str, worker_id: str,
                             baseline_commit: str, result_commit: str,
                             patch_sha256: str | None) -> None:
        """Insert one source receipt after verified completed ACK, never replace."""
        try:
            with self._db() as db:
                row = db.execute("""SELECT s.state,d.status,d.result,c.worker_id
                    FROM task_states s
                    LEFT JOIN task_deliveries d ON d.task_id=s.task_id
                    LEFT JOIN task_claims c ON c.task_id=s.task_id
                    WHERE s.task_id=?""", (task_id,)).fetchone()
                if row != ("succeeded", "completed", "success", worker_id):
                    raise StorageUnavailable("source version requires completed task")
                db.execute("""INSERT INTO task_source_versions
                    (task_id,worker_id,baseline_commit,result_commit,patch_sha256)
                    VALUES (?,?,?,?,?)""",
                    (task_id,worker_id,baseline_commit,result_commit,patch_sha256))
        except sqlite3.IntegrityError as exc:
            raise StorageUnavailable("source version already exists or is invalid") from exc
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist source version") from exc

    def read_source_version(self, task_id: str) -> tuple | None:
        """The caller must verify owner/project scope before using this method."""
        try:
            return self._db().execute("""SELECT worker_id,baseline_commit,
                result_commit,patch_sha256,recorded_at
                FROM task_source_versions WHERE task_id=?""",
                (task_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot inspect source version") from exc

    def process_record(self, task_id: str) -> tuple | None:
        try:
            return self._db().execute("""SELECT worker_id,pid,pgid,sid,
                start_ticks,boot_id,launch_kind,status FROM task_process_runs
                WHERE task_id=?""", (task_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot inspect process ledger") from exc

    def register_process(self, task_id: str, worker_id: str, *, pid: int,
                         pgid: int, sid: int, start_ticks: int,
                         boot_id: str, launch_kind: str) -> None:
        """Commit process identity before allowing a gated worker to run."""
        try:
            with self._db() as db:
                old = self.process_record(task_id)
                if old is not None and old[-1] != "stopped":
                    raise StorageUnavailable("previous process is not confirmed stopped")
                db.execute("""INSERT INTO task_process_runs
                    (task_id,worker_id,pid,pgid,sid,start_ticks,boot_id,
                     launch_kind,status) VALUES (?,?,?,?,?,?,?,?,'active')
                    ON CONFLICT(task_id) DO UPDATE SET
                    worker_id=excluded.worker_id,pid=excluded.pid,
                    pgid=excluded.pgid,sid=excluded.sid,
                    start_ticks=excluded.start_ticks,boot_id=excluded.boot_id,
                    launch_kind=excluded.launch_kind,status='active',
                    recorded_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                    stopped_at=NULL""",
                    (task_id, worker_id, pid, pgid, sid, start_ticks,
                     boot_id, launch_kind))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot record managed process") from exc

    def mark_process_stopped(self, task_id: str, pid: int,
                             start_ticks: int, boot_id: str) -> None:
        try:
            with self._db() as db:
                result = db.execute("""UPDATE task_process_runs SET
                    status='stopped',stopped_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE task_id=? AND pid=? AND start_ticks=?
                      AND boot_id=? AND status='active'""",
                    (task_id, pid, start_ticks, boot_id))
                if result.rowcount != 1:
                    raise StorageUnavailable("process identity was not active")
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot settle process ledger") from exc

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

    @staticmethod
    def _create_owner_approval_table(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS task_owner_approvals (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL,
            channel TEXT NOT NULL CHECK(channel IN ('message','button')),
            proposal_sha256 TEXT NOT NULL CHECK(length(proposal_sha256) = 64),
            approved_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(owner_id,project_id,proposal_id)
        )""")

    def owner_approval(self, task_id: str) -> tuple | None:
        try:
            return self._db().execute("""SELECT owner_id,project_id,proposal_id,
                channel,proposal_sha256 FROM task_owner_approvals WHERE task_id=?""",
                (task_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read owner approval") from exc

    @staticmethod
    def _create_plan_table(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS approved_task_plans (
            plan_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1000000),
            proposal_id TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            channel TEXT NOT NULL CHECK(channel IN ('message','button')),
            plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256) = 64),
            canonical_json TEXT NOT NULL,
            approved_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(plan_id, revision)
        )""")

    def save_approved_plan(self, *, plan_id: str, revision: int,
                           proposal_id: str, owner_id: str, project_id: str,
                           channel: str, plan_sha256: str,
                           canonical_json: str) -> tuple:
        """Atomic version CAS; replays must match exactly, never revive old plans.

        No task is inserted or dispatched by this operation. Only the trusted
        caller may provide an authenticated owner decision.
        """
        try:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            with db:
                latest = db.execute("""SELECT plan_id,revision,proposal_id,owner_id,
                    project_id,channel,plan_sha256,canonical_json
                    FROM approved_task_plans WHERE plan_id=?
                    ORDER BY revision DESC LIMIT 1""", (plan_id,)).fetchone()
                row = (plan_id, revision, proposal_id, owner_id, project_id,
                       channel, plan_sha256, canonical_json)
                if latest is not None:
                    if latest[3:5] != (owner_id, project_id):
                        raise StorageUnavailable("plan unavailable in owner scope")
                    if latest == row:
                        return latest
                    if revision != latest[1] + 1:
                        raise StorageUnavailable("plan revision is stale or out of order")
                elif revision != 1:
                    raise StorageUnavailable("first plan revision must be one")
                db.execute("""INSERT INTO approved_task_plans
                    (plan_id,revision,proposal_id,owner_id,project_id,channel,
                     plan_sha256,canonical_json) VALUES (?,?,?,?,?,?,?,?)""", row)
                return row
        except sqlite3.IntegrityError as exc:
            raise StorageUnavailable("plan approval conflicts with existing receipt") from exc
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist approved plan") from exc

    def latest_approved_plan(self, plan_id: str) -> tuple | None:
        try:
            return self._db().execute("""SELECT plan_id,revision,proposal_id,owner_id,
                project_id,channel,plan_sha256,canonical_json
                FROM approved_task_plans WHERE plan_id=?
                ORDER BY revision DESC LIMIT 1""", (plan_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read approved plan") from exc

    @staticmethod
    def _create_specialist_tables(db: sqlite3.Connection) -> None:
        """Versioned approvals never imply grants or task/process launches."""
        db.execute("""CREATE TABLE IF NOT EXISTS approved_specialist_catalogs (
            owner_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1000000),
            proposal_id TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL CHECK(channel IN ('message','button')),
            digest TEXT NOT NULL CHECK(length(digest)=64),
            canonical_json TEXT NOT NULL,
            approved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(owner_id, revision)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS approved_specialist_teams (
            team_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1000000),
            proposal_id TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            channel TEXT NOT NULL CHECK(channel IN ('message','button')),
            digest TEXT NOT NULL CHECK(length(digest)=64),
            canonical_json TEXT NOT NULL,
            approved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(team_id, revision)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_specialist_pins (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL,
            plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
            step_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            team_revision INTEGER NOT NULL,
            team_digest TEXT NOT NULL CHECK(length(team_digest)=64),
            catalog_revision INTEGER NOT NULL,
            catalog_digest TEXT NOT NULL CHECK(length(catalog_digest)=64),
            agent_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            provider_reason TEXT,
            allowed_actions TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_specialist_audit (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
            reason_code TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_permission_revocations (
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            worker_id TEXT NOT NULL,
            action TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(task_id, worker_id, action)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS project_emergency_holds (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0,1)),
            PRIMARY KEY(owner_id,project_id)
        )""")

    @staticmethod
    def _create_transfer_tables(db: sqlite3.Connection) -> None:
        """v14: immutable proposals, attempt chain and integrity-bound artifacts.

        No new provider process is authorized by a row in these tables.
        Existing task claims and terminal task states remain unchanged.
        """
        db.execute("""CREATE TABLE IF NOT EXISTS task_transfer_proposals (
            proposal_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            proposal_sha256 TEXT NOT NULL CHECK(length(proposal_sha256)=64),
            body_json TEXT NOT NULL,
            expires_at REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('proposed','approved')),
            approved_channel TEXT CHECK(approved_channel IN ('button','message')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_transfer_attempts (
            attempt_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 10000),
            agent_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('retired','waiting','ready','running','succeeded',
                 'failed','cancelled','timed_out','superseded','held')),
            source_proposal_id TEXT REFERENCES task_transfer_proposals(proposal_id),
            checkpoint_sha256 TEXT NOT NULL CHECK(length(checkpoint_sha256)=64),
            team_revision INTEGER NOT NULL,
            team_sha256 TEXT NOT NULL CHECK(length(team_sha256)=64),
            role_actions_json TEXT NOT NULL,
            wait_started_at REAL,
            wait_checks INTEGER NOT NULL DEFAULT 0 CHECK(wait_checks BETWEEN 0 AND 100000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(task_id,ordinal)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_transfer_current (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            attempt_id TEXT NOT NULL UNIQUE REFERENCES task_transfer_attempts(attempt_id),
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 10000)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_transfer_artifacts (
            attempt_id TEXT NOT NULL REFERENCES task_transfer_attempts(attempt_id),
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(length(sha256)=64),
            status TEXT NOT NULL CHECK(status IN ('verified','unverified')),
            evidence_sha256 TEXT CHECK(evidence_sha256 IS NULL OR length(evidence_sha256)=64),
            body BLOB NOT NULL CHECK(length(body) <= 2097152),
            PRIMARY KEY(attempt_id,path)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_transfer_audit (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            proposal_id TEXT,
            attempt_id TEXT,
            action TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('allowed','denied','held')),
            reason_code TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")

    @staticmethod
    def _create_crisis_tables(db: sqlite3.Connection) -> None:
        """v15: exactly one crisis subtask per independently recorded defect."""
        db.execute("""CREATE TABLE IF NOT EXISTS task_crisis_escalations (
            incident_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            parent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
            child_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
            original_agent TEXT NOT NULL,
            crisis_agent TEXT NOT NULL,
            crisis_provider TEXT NOT NULL,
            crisis_role TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64),
            team_sha256 TEXT NOT NULL CHECK(length(team_sha256)=64),
            catalog_sha256 TEXT NOT NULL CHECK(length(catalog_sha256)=64),
            step_id TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
            stop_reason TEXT NOT NULL CHECK(stop_reason IN ('stalled','exhausted')),
            deadline_at REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('reserved','assigned','ready_for_parent','hold')),
            outcome_sha256 TEXT CHECK(outcome_sha256 IS NULL OR length(outcome_sha256)=64),
            reason_code TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            CHECK(parent_task_id <> child_task_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_crisis_audit (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id TEXT NOT NULL REFERENCES task_crisis_escalations(incident_id),
            action TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")

    def crisis_row(self, incident_id: str) -> tuple | None:
        try:
            return self._db().execute("""SELECT incident_id,owner_id,project_id,
                parent_task_id,child_task_id,original_agent,crisis_agent,
                crisis_provider,crisis_role,plan_sha256,team_sha256,
                catalog_sha256,step_id,manifest_sha256,stop_reason,deadline_at,
                status,outcome_sha256,reason_code
                FROM task_crisis_escalations WHERE incident_id=?""",
                (incident_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read crisis escalation") from exc

    def crisis_child(self, task_id: str) -> bool:
        try:
            return self._db().execute("""SELECT 1 FROM task_crisis_escalations
                WHERE child_task_id=?""", (task_id,)).fetchone() is not None
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot inspect crisis child") from exc

    def crisis_child_state(self, task_id: str) -> tuple[str, float] | None:
        try:
            return self._db().execute("""SELECT status,deadline_at
                FROM task_crisis_escalations WHERE child_task_id=?""",
                (task_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot inspect crisis permissions") from exc

    def insert_crisis(self, row: tuple, evidence_sha256: str) -> None:
        try:
            with self._db() as db:
                db.execute("""INSERT INTO task_crisis_escalations
                    (incident_id,owner_id,project_id,parent_task_id,child_task_id,
                     original_agent,crisis_agent,crisis_provider,crisis_role,
                     plan_sha256,team_sha256,catalog_sha256,step_id,
                     manifest_sha256,stop_reason,deadline_at,status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'reserved')""", row)
                db.execute("""INSERT INTO task_crisis_audit
                    (incident_id,action,evidence_sha256) VALUES (?,?,?)""",
                    (row[0], "reserved", evidence_sha256))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot reserve crisis escalation") from exc

    def advance_crisis(self, incident_id: str, *, from_status: str,
                       to_status: str, outcome_sha256: str,
                       reason_code: str) -> None:
        try:
            with self._db() as db:
                updated = db.execute("""UPDATE task_crisis_escalations
                    SET status=?,outcome_sha256=?,reason_code=?
                    WHERE incident_id=? AND status=?""",
                    (to_status,outcome_sha256,reason_code,incident_id,
                     from_status)).rowcount
                if updated != 1:
                    raise StorageUnavailable("crisis transition changed")
                db.execute("""INSERT INTO task_crisis_audit
                    (incident_id,action,evidence_sha256) VALUES (?,?,?)""",
                    (incident_id,to_status,outcome_sha256))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot commit crisis transition") from exc

    def crisis_audit(self, incident_id: str) -> tuple[tuple, ...]:
        try:
            return tuple(self._db().execute("""SELECT action,evidence_sha256
                FROM task_crisis_audit WHERE incident_id=? ORDER BY event_id""",
                (incident_id,)).fetchall())
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read crisis audit") from exc

    @staticmethod
    def _create_global_lead_tables(db: sqlite3.Connection) -> None:
        """v16: owner decisions, not permissions or provider dispatch."""
        db.execute("""CREATE TABLE IF NOT EXISTS global_direction_decisions (
            owner_id TEXT NOT NULL,
            direction_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1000000),
            action TEXT NOT NULL CHECK(action IN ('appoint','remove')),
            agent_id TEXT,
            role_id TEXT,
            catalog_revision INTEGER NOT NULL,
            catalog_sha256 TEXT,
            previous_sha256 TEXT,
            proposal_id TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL CHECK(channel IN ('message','button')),
            digest TEXT NOT NULL CHECK(length(digest)=64),
            canonical_json TEXT NOT NULL,
            approved_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(owner_id,direction_id,revision),
            CHECK((action='appoint' AND agent_id IS NOT NULL
                AND role_id IS NOT NULL AND catalog_revision>0
                AND length(catalog_sha256)=64) OR
                  (action='remove' AND agent_id IS NULL
                AND role_id IS NULL AND catalog_revision=0
                AND catalog_sha256 IS NULL))
        )""")

    def global_lead_rows(self, owner_id: str,
                         direction_id: str | None = None) -> tuple[tuple, ...]:
        try:
            return tuple(self._db().execute("""SELECT owner_id,direction_id,
                revision,action,agent_id,role_id,catalog_revision,
                catalog_sha256,previous_sha256,proposal_id,channel,digest,
                canonical_json FROM global_direction_decisions
                WHERE owner_id=? AND (? IS NULL OR direction_id=?)
                ORDER BY direction_id,revision""",
                (owner_id,direction_id,direction_id)).fetchall())
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot inspect global direction leaders") from exc

    def save_global_lead(self, row: tuple, expected_previous: str | None,
                         *, catalog_digest: str | None) -> None:
        """Atomic CAS against latest appointment and approved catalog."""
        (owner,direction,revision,action,agent,role,cat_rev,cat_sha,
         previous,proposal_id,channel,digest,canonical) = row
        try:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            with db:
                latest = db.execute("""SELECT revision,digest FROM
                    global_direction_decisions WHERE owner_id=? AND direction_id=?
                    ORDER BY revision DESC LIMIT 1""",
                    (owner,direction)).fetchone()
                if (revision != (latest[0]+1 if latest else 1)
                        or expected_previous != (latest[1] if latest else None)
                        or previous != expected_previous):
                    raise StorageUnavailable("stale global lead decision")
                if action == "appoint":
                    current = db.execute("""SELECT revision,digest FROM
                        approved_specialist_catalogs WHERE owner_id=?
                        ORDER BY revision DESC LIMIT 1""",
                        (owner,)).fetchone()
                    if current != (cat_rev,catalog_digest) or cat_sha != catalog_digest:
                        raise StorageUnavailable("global lead catalog changed")
                db.execute("""INSERT INTO global_direction_decisions
                    (owner_id,direction_id,revision,action,agent_id,role_id,
                     catalog_revision,catalog_sha256,previous_sha256,
                     proposal_id,channel,digest,canonical_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", row)
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot save global lead decision") from exc

    def transfer_has_started(self, task_id: str) -> bool:
        """Whether v14 has superseded the immutable legacy assignment."""
        try:
            return self._db().execute("SELECT 1 FROM task_transfer_current "
                                      "WHERE task_id=?", (task_id,)).fetchone() is not None
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot inspect transfer state") from exc

    def save_specialist_approval(self, *, kind: str, key: str, revision: int,
                                  proposal_id: str, owner_id: str,
                                  project_id: str | None, channel: str,
                                  digest: str, canonical_json: str) -> tuple:
        """Immutable exact-version receipt; stale and cross-owner CAS rejected."""
        if kind not in ("catalog", "team"):
            raise ValueError("unknown specialist approval")
        table = ("approved_specialist_catalogs" if kind == "catalog"
                 else "approved_specialist_teams")
        try:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            with db:
                if kind == "catalog":
                    latest = db.execute(f"""SELECT owner_id,revision,proposal_id,
                        channel,digest,canonical_json FROM {table}
                        WHERE owner_id=? ORDER BY revision DESC LIMIT 1""",
                        (owner_id,)).fetchone()
                    row = (owner_id,revision,proposal_id,channel,digest,canonical_json)
                else:
                    latest = db.execute(f"""SELECT team_id,revision,proposal_id,
                        owner_id,project_id,channel,digest,canonical_json
                        FROM {table} WHERE team_id=? ORDER BY revision DESC LIMIT 1""",
                        (key,)).fetchone()
                    row = (key,revision,proposal_id,owner_id,project_id,
                           channel,digest,canonical_json)
                if latest is not None:
                    if kind == "team" and latest[3:5] != (owner_id,project_id):
                        raise StorageUnavailable("specialist team unavailable in owner scope")
                    if latest == row:
                        return latest
                    if revision != latest[1] + 1:
                        raise StorageUnavailable("specialist revision stale or out of order")
                elif revision != 1:
                    raise StorageUnavailable("first specialist revision must be one")
                placeholders = ",".join("?" for _ in row)
                columns = ("owner_id,revision,proposal_id,channel,digest,canonical_json"
                           if kind == "catalog" else
                           "team_id,revision,proposal_id,owner_id,project_id,"
                           "channel,digest,canonical_json")
                db.execute(f"INSERT INTO {table} ({columns}) "
                           f"VALUES ({placeholders})", row)
                return row
        except sqlite3.IntegrityError as exc:
            raise StorageUnavailable("specialist approval conflicts") from exc
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist specialist approval") from exc

    def specialist_catalog(self, owner_id: str, revision: int | None = None) -> tuple | None:
        try:
            return self._db().execute("""SELECT owner_id,revision,proposal_id,
                channel,digest,canonical_json FROM approved_specialist_catalogs
                WHERE owner_id=? AND (? IS NULL OR revision=?)
                ORDER BY revision DESC LIMIT 1""",
                (owner_id,revision,revision)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read specialist catalog") from exc

    def specialist_team(self, team_id: str, revision: int | None = None) -> tuple | None:
        try:
            return self._db().execute("""SELECT team_id,revision,proposal_id,
                owner_id,project_id,channel,digest,canonical_json
                FROM approved_specialist_teams WHERE team_id=?
                AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1""",
                (team_id,revision,revision)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read specialist team") from exc

    def specialist_pin(self, task_id: str) -> tuple | None:
        try:
            return self._db().execute("""SELECT task_id,owner_id,project_id,
                plan_id,plan_revision,plan_digest,step_id,team_id,
                team_revision,team_digest,catalog_revision,catalog_digest,
                agent_id,role_id,provider_id,provider_reason,allowed_actions
                FROM task_specialist_pins WHERE task_id=?""",
                (task_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read specialist assignment") from exc

    def specialist_audit(self, task_id: str) -> list[tuple]:
        try:
            return self._db().execute("""SELECT event_id,agent_id,accepted,
                reason_code,occurred_at FROM task_specialist_audit
                WHERE task_id=? ORDER BY event_id""", (task_id,)).fetchall()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read specialist audit") from exc

    def reject_specialist(self, task_id: str, owner_id: str, project_id: str,
                          agent_id: str, reason: str) -> None:
        try:
            with self._db() as db:
                db.execute("""INSERT INTO task_specialist_audit
                    (task_id,owner_id,project_id,agent_id,accepted,reason_code)
                    VALUES (?,?,?,?,0,?)""",
                    (task_id,owner_id,project_id,agent_id,reason))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot audit rejected specialist") from exc

    def is_permission_revoked(self, task_id: str, worker_id: str,
                              action: str) -> bool:
        try:
            return self._db().execute("""SELECT 1 FROM task_permission_revocations
                WHERE task_id=? AND worker_id=? AND action=?""",
                (task_id,worker_id,action)).fetchone() is not None
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot verify permission revocation") from exc

    def revoke_permission(self, task_id: str, worker_id: str, action: str) -> None:
        try:
            with self._db() as db:
                db.execute("""INSERT OR IGNORE INTO task_permission_revocations
                    (task_id,worker_id,action) VALUES (?,?,?)""",
                    (task_id,worker_id,action))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot revoke permission") from exc

    def set_emergency_hold(self, owner_id: str, project_id: str, active: bool) -> None:
        try:
            with self._db() as db:
                db.execute("""INSERT INTO project_emergency_holds
                    (owner_id,project_id,active) VALUES (?,?,?)
                    ON CONFLICT(owner_id,project_id) DO UPDATE SET active=excluded.active""",
                    (owner_id,project_id,int(active)))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot change project hold") from exc

    def emergency_held(self, owner_id: str, project_id: str) -> bool:
        try:
            row = self._db().execute("""SELECT active FROM project_emergency_holds
                WHERE owner_id=? AND project_id=?""", (owner_id,project_id)).fetchone()
            return row is not None and row[0] == 1
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot verify project hold") from exc

    def insert_task(self, record, *, key_hash: str | None = None,
                    payload_hash: str | None = None,
                    owner_approval: tuple[str, str, str] | None = None) -> None:
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
                if key_hash is not None and payload_hash is not None:
                    db.execute("""INSERT INTO task_admission_keys
                        (owner_id,project_id,key_hash,payload_hash,task_id)
                        VALUES (?,?,?,?,?)""", (record.owner_id, record.project_id,
                                                  key_hash, payload_hash, record.task_id))
                if owner_approval is not None:
                    proposal_id, channel, proposal_sha256 = owner_approval
                    db.execute("""INSERT INTO task_owner_approvals
                        (task_id,owner_id,project_id,proposal_id,channel,proposal_sha256)
                        VALUES (?,?,?,?,?,?)""", (record.task_id,record.owner_id,
                            record.project_id,proposal_id,channel,proposal_sha256))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist task") from exc

    def admission_key(self, owner_id: str, project_id: str,
                      key_hash: str) -> tuple[str, str] | None:
        try:
            return self._db().execute("""SELECT task_id,payload_hash
                FROM task_admission_keys
                WHERE owner_id=? AND project_id=? AND key_hash=?""",
                (owner_id, project_id, key_hash)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read admission key") from exc

    def delivery(self, task_id: str) -> tuple | None:
        try:
            return self._db().execute("""SELECT max_attempts,attempts_used,
                attempt_token,status,retry_at,result
                FROM task_deliveries WHERE task_id=?""", (task_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read delivery") from exc

    def begin_delivery(self, task_id: str, max_attempts: int,
                       token: str, now: float) -> tuple[tuple, bool]:
        """Under inbox lock: commit a new bounded attempt or replay existing."""
        try:
            with self._db() as db:
                existing = self.delivery(task_id)
                if existing is None:
                    db.execute("""INSERT INTO task_deliveries
                        (task_id,max_attempts,attempts_used,attempt_token,status)
                        VALUES (?,?,1,?,'active')""", (task_id, max_attempts, token))
                    return (max_attempts, 1, token, "active", 0.0, None), True
                limit, used, old_token, status, retry_at, result = existing
                if limit != max_attempts:
                    raise StorageUnavailable("delivery policy cannot be changed")
                if status != "retryable" or now < retry_at:
                    return existing, False
                if used >= limit:
                    raise StorageUnavailable("invalid retryable delivery")
                updated = db.execute("""UPDATE task_deliveries SET
                    attempts_used=?,attempt_token=?,status='active',retry_at=0,result=NULL
                    WHERE task_id=? AND attempt_token=? AND status='retryable'""",
                    (used + 1, token, task_id, old_token))
                if updated.rowcount != 1:
                    raise StorageUnavailable("delivery changed unexpectedly")
                return (limit, used + 1, token, "active", 0.0, None), True
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot reserve task delivery") from exc

    def settle_delivery(self, task_id: str, token: str, success: bool,
                        retry_at: float) -> tuple[tuple, bool]:
        """Persist one ACK; repeated ACK never counts as another attempt."""
        try:
            with self._db() as db:
                current = self.delivery(task_id)
                if current is None:
                    return None, False
                limit, used, old_token, status, _, result = current
                if old_token != token:
                    return current, False
                if status != "active":
                    if ((status == "completed" and success)
                            or (status in ("retryable", "exhausted") and not success)):
                        return current, False
                    return current, False
                next_status = ("completed" if success else
                               "exhausted" if used >= limit else "retryable")
                outcome = "success" if success else "failure"
                updated = db.execute("""UPDATE task_deliveries SET
                    status=?,retry_at=?,result=?
                    WHERE task_id=? AND attempt_token=? AND status='active'""",
                    (next_status, retry_at if next_status == "retryable" else 0,
                     outcome, task_id, token))
                if updated.rowcount != 1:
                    raise StorageUnavailable("delivery changed unexpectedly")
                return (limit, used, token, next_status,
                        retry_at if next_status == "retryable" else 0, outcome), True
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot settle task delivery") from exc

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

    def insert_assignment(self, assignment, *, specialist_pin: tuple | None = None) -> None:
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
                if specialist_pin is not None:
                    db.execute("""INSERT INTO task_specialist_pins
                        (task_id,owner_id,project_id,plan_id,plan_revision,
                         plan_digest,step_id,team_id,team_revision,team_digest,
                         catalog_revision,catalog_digest,agent_id,role_id,
                         provider_id,provider_reason,allowed_actions)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", specialist_pin)
                    db.execute("""INSERT INTO task_specialist_audit
                        (task_id,owner_id,project_id,agent_id,accepted,reason_code)
                        VALUES (?,?,?,?,1,'assigned')""",
                        (specialist_pin[0],specialist_pin[1],
                         specialist_pin[2],specialist_pin[12]))
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

    def write_permission_audit(self, task_id: str, action: str, allowed: bool,
                               reason_code: str, principal_kind: str) -> None:
        """Durable allow/deny decision before any sensitive callback."""
        try:
            with self._db() as db:
                db.execute("""INSERT INTO task_permission_audit
                    (task_id,action,allowed,reason_code,principal_kind)
                    VALUES (?,?,?,?,?)""", (task_id, action, int(allowed),
                                             reason_code, principal_kind))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist permission decision") from exc

    def permission_history(self, task_id: str) -> list[tuple]:
        """Only task ID, action category, result, and generic reason."""
        try:
            return self._db().execute("""SELECT event_id,task_id,action,
                allowed,reason_code,principal_kind,occurred_at
                FROM task_permission_audit WHERE task_id=? ORDER BY event_id""",
                (task_id,)).fetchall()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read permission audit") from exc

    def write_provider_audit(self, task_id: str, model_id: str,
                             outcome: str, *, provider: str = "gpt") -> None:
        """Persist bounded provider status, never prompt, response or token."""
        try:
            with self._db() as db:
                db.execute("""INSERT INTO task_provider_audit
                    (task_id,provider,model_id,outcome) VALUES (?,?,?,?)""",
                    (task_id, provider, model_id, outcome))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist provider outcome") from exc

    def provider_history(self, task_id: str) -> list[tuple]:
        try:
            return self._db().execute("""SELECT event_id,task_id,provider,
                model_id,outcome,occurred_at FROM task_provider_audit
                WHERE task_id=? ORDER BY event_id""", (task_id,)).fetchall()
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot read provider outcomes") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
