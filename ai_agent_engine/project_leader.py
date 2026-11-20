"""Stage 7.5: owner-approved, isolated project-leader workspace foundation.

This is a trusted local storage bridge, not an HTTP endpoint, an authentication
adapter, a model runner or a task permission grant.  The project proposal is
inert until a separately authenticated caller supplies an exact OwnerDecision.
The central task SQLite, task scheduler and permission gate are not changed.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
from threading import RLock
from uuid import uuid4

from .main_orchestrator import OwnerDecision
from .task_api import OwnerScope, ProjectForbidden, _PROJECT_ID, _SHA256


REGISTRY_APPLICATION_ID = 0x41454C52  # AELR, distinct from task/engine SQLite.
PROJECT_APPLICATION_ID = 0x41454C50  # AELP, per-project state.
SCHEMA_VERSION = 1
PROJECT_SCHEMA_VERSION = 2  # v1 management state; v2 adds offline 7.6 memory.
KINDS = frozenset({"plan", "task", "decision_link", "qa_report", "error",
                   "checkpoint", "history", "context_link", "source_link"})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)


class InvalidProjectLeader(ValueError):
    """Invalid proposal or owner decision, without echoing private content."""


class ProjectLeaderDenied(PermissionError):
    """Project access or a conflicting approval is denied."""


class ProjectLeaderUnavailable(RuntimeError):
    """Private workspace is absent, unsafe, damaged or partly provisioned."""


@dataclass(frozen=True, slots=True)
class ProjectProposal:
    proposal_id: str
    owner_id: str
    project_id: str
    title: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ProjectWorkspace:
    owner_id: str
    project_id: str
    title: str
    project_directory: Path
    database: Path
    documents_directory: Path


@dataclass(frozen=True, slots=True)
class ManagementReference:
    reference_id: str
    kind: str
    source_ref: str
    checksum: str | None
    verified: bool


def _digest(owner_id: str, project_id: str, title: str) -> str:
    canonical = json.dumps({"schema_version": SCHEMA_VERSION,
                            "owner_id": owner_id, "project_id": project_id,
                            "title": title}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return sha256(canonical).hexdigest()


def _title(value: object) -> str:
    if (type(value) is not str or not 1 <= len(value) <= 128
            or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise InvalidProjectLeader("invalid project title")
    return value


def draft_project(scope: OwnerScope, project_id: str, title: str) -> ProjectProposal:
    """Create an inert proposal; a model cannot activate a project with it."""
    if (not isinstance(scope, OwnerScope) or type(project_id) is not str
            or not _PROJECT_ID.fullmatch(project_id)):
        raise InvalidProjectLeader("invalid project proposal")
    if project_id not in scope.project_ids:
        raise ProjectForbidden("project outside configured scope")
    title = _title(title)
    return ProjectProposal("proposal_" + uuid4().hex, scope.owner_id,
                           project_id, title,
                           _digest(scope.owner_id, project_id, title))


def _check_directory(fd: int) -> None:
    metadata = os.fstat(fd)
    if (not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ProjectLeaderUnavailable("project directory must be private 0700")


def _directory(parent_fd: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        _check_directory(fd)
        return fd
    except Exception:
        os.close(fd)
        raise


def _file(parent_fd: int, name: str, *, create: bool) -> int:
    fd = os.open(name, _FILE_FLAGS | (os.O_CREAT if create else 0),
                 0o600, dir_fd=parent_fd)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ProjectLeaderUnavailable("project database must be private 0600")
        return fd
    except Exception:
        os.close(fd)
        raise


def _database(path: Path) -> sqlite3.Connection:
    # Registry access is serialized by ProjectLeaderStore._mutex; worker
    # threads may reuse this connection through that trusted boundary.
    db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _registry_schema(db: sqlite3.Connection) -> None:
    app = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if (app, version) == (0, 0):
        if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise ProjectLeaderUnavailable("unrecognized registry database")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE projects (
                owner_id TEXT NOT NULL, project_id TEXT NOT NULL,
                title TEXT NOT NULL, proposal_id TEXT NOT NULL UNIQUE,
                proposal_sha256 TEXT NOT NULL, approval_channel TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('provisioning','active')),
                PRIMARY KEY(owner_id,project_id))""")
            db.execute(f"PRAGMA application_id={REGISTRY_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif (app, version) != (REGISTRY_APPLICATION_ID, SCHEMA_VERSION):
        raise ProjectLeaderUnavailable("unrecognized registry database")
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ProjectLeaderUnavailable("damaged registry database")


def _project_schema(db: sqlite3.Connection, identity: tuple[str, ...],
                    *, create: bool) -> None:
    app = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if (app, version) == (0, 0) and create:
        if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise ProjectLeaderUnavailable("unrecognized project database")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE identity (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                owner_id TEXT NOT NULL, project_id TEXT NOT NULL,
                title TEXT NOT NULL, proposal_id TEXT NOT NULL,
                proposal_sha256 TEXT NOT NULL, approval_channel TEXT NOT NULL)""")
            db.execute("""CREATE TABLE management_refs (
                reference_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                source_ref TEXT NOT NULL, checksum TEXT,
                verified INTEGER NOT NULL CHECK(verified IN (0,1)))""")
            db.execute("INSERT INTO identity VALUES (1,?,?,?,?,?,?)", identity)
            db.execute(f"PRAGMA application_id={PROJECT_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif (app, version) not in ((PROJECT_APPLICATION_ID, SCHEMA_VERSION),
                               (PROJECT_APPLICATION_ID, PROJECT_SCHEMA_VERSION)):
        raise ProjectLeaderUnavailable("unrecognized project database")
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ProjectLeaderUnavailable("damaged project database")
    row = db.execute("""SELECT owner_id,project_id,title,proposal_id,
        proposal_sha256,approval_channel FROM identity WHERE singleton=1""").fetchone()
    if row != identity:
        raise ProjectLeaderUnavailable("project identity does not match approval")
    if version == SCHEMA_VERSION or (app, version) == (0, 0):
        # One atomic, backwards-compatible migration.  Never recreate identity
        # or management_refs and never touch the central task database.
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE memory_events (
                event_id TEXT PRIMARY KEY,
                event_kind TEXT NOT NULL CHECK(event_kind IN
                    ('decision','plan','task','qa','error','checkpoint','lesson','source')),
                body TEXT NOT NULL, source_ref TEXT NOT NULL,
                source_sha256 TEXT,
                status TEXT NOT NULL CHECK(status IN ('proposed','verified')),
                supersedes_id TEXT UNIQUE REFERENCES memory_events(event_id),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("""CREATE INDEX memory_lookup
                ON memory_events(status,event_kind,created_at)""")
            db.execute("""CREATE TABLE memory_summaries (
                revision INTEGER PRIMARY KEY CHECK(revision >= 1),
                body TEXT NOT NULL, event_ids TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE memory_audit (
                audit_id TEXT PRIMARY KEY, action TEXT NOT NULL,
                record_ref TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(action,record_ref))""")
            db.execute(f"PRAGMA user_version={PROJECT_SCHEMA_VERSION}")


class ProjectLeaderStore:
    """Local project provisioning and minimal owner-scoped reference records.

    The caller supplies an existing trusted private ``projects`` directory.
    This store has its own registry; it neither migrates nor copies task SQLite.
    No specialist role API, memory prompt builder or autonomous recovery is
    activated until its later roadmap stage.
    """

    def __init__(self, projects_root: str | Path) -> None:
        self.root = Path(projects_root).absolute()
        self._mutex = RLock()
        self._root_fd: int | None = None
        self._registry_fd: int | None = None
        self._registry: sqlite3.Connection | None = None
        try:
            self._root_fd = os.open(self.root, _DIR_FLAGS)
            _check_directory(self._root_fd)
            self._registry_fd = _file(self._root_fd, "project-leaders.sqlite3",
                                      create=True)
            fcntl.flock(self._registry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._registry = _database(self.root / "project-leaders.sqlite3")
            _registry_schema(self._registry)
        except (OSError, sqlite3.Error, BlockingIOError) as exc:
            self.close()
            raise ProjectLeaderUnavailable("cannot open private project registry") from exc
        except Exception:
            self.close()
            raise

    def _db(self) -> sqlite3.Connection:
        if self._registry is None:
            raise ProjectLeaderUnavailable("project registry is closed")
        return self._registry

    def _ensure_project(self, identity: tuple[str, ...], *, create: bool) -> ProjectWorkspace:
        owner_id, project_id, title, _, _, _ = identity
        assert self._root_fd is not None
        owner_fd = _directory(self._root_fd, owner_id, create=create)
        try:
            project_fd = _directory(owner_fd, project_id, create=create)
            try:
                documents_fd = _directory(project_fd, "documents", create=create)
                os.close(documents_fd)
                database_fd = _file(project_fd, "leader.sqlite3", create=create)
                try:
                    db = _database(self.root / owner_id / project_id / "leader.sqlite3")
                    try:
                        _project_schema(db, identity, create=create)
                    finally:
                        db.close()
                finally:
                    os.close(database_fd)
            finally:
                os.close(project_fd)
        finally:
            os.close(owner_fd)
        directory = self.root / owner_id / project_id
        return ProjectWorkspace(owner_id, project_id, title, directory,
                                directory / "leader.sqlite3", directory / "documents")

    @staticmethod
    def _authorize(scope: OwnerScope, project_id: str) -> None:
        if (not isinstance(scope, OwnerScope) or type(project_id) is not str
                or not _PROJECT_ID.fullmatch(project_id)):
            raise InvalidProjectLeader("invalid project access")
        if project_id not in scope.project_ids:
            raise ProjectForbidden("project outside configured scope")

    def approve(self, scope: OwnerScope, proposal: ProjectProposal,
                decision: OwnerDecision) -> ProjectWorkspace:
        """Durably bind exact owner approval, then idempotently provision files.

        A process crash after the registry insert leaves ``provisioning``;
        retry with the SAME approval repairs it without deleting user files.
        """
        if (not isinstance(scope, OwnerScope)
                or not isinstance(proposal, ProjectProposal)
                or not isinstance(decision, OwnerDecision)
                or proposal.owner_id != scope.owner_id
                or decision.owner_id != scope.owner_id
                or type(proposal.proposal_id) is not str
                or not proposal.proposal_id.startswith("proposal_")
                or len(proposal.proposal_id) != 41
                or any(c not in "0123456789abcdef" for c in proposal.proposal_id[9:])
                or type(proposal.project_id) is not str
                or not _PROJECT_ID.fullmatch(proposal.project_id)
                or decision.proposal_id != proposal.proposal_id
                or decision.proposal_sha256 != proposal.sha256
                or type(proposal.sha256) is not str
                or not _SHA256.fullmatch(proposal.sha256)
                or decision.channel not in ("message", "button")
                or type(decision.channel) is not str
                or _digest(proposal.owner_id, proposal.project_id,
                           _title(proposal.title)) != proposal.sha256):
            raise InvalidProjectLeader("owner approval does not match project")
        self._authorize(scope, proposal.project_id)
        identity = (proposal.owner_id, proposal.project_id, proposal.title,
                    proposal.proposal_id, proposal.sha256, decision.channel)
        with self._mutex:
            db = self._db()
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("""SELECT owner_id,project_id,title,
                        proposal_id,proposal_sha256,approval_channel,state
                        FROM projects WHERE owner_id=? AND project_id=?""",
                        (proposal.owner_id, proposal.project_id)).fetchone()
                    if row is None:
                        db.execute("""INSERT INTO projects VALUES
                            (?,?,?,?,?,?,'provisioning')""", identity)
                    elif row[:6] != identity:
                        raise ProjectLeaderDenied("project already approved differently")
                # An active project whose files vanished must fail closed;
                # only an interrupted provisioning can initialize missing files.
                workspace = self._ensure_project(
                    identity, create=(row is None or row[6] == "provisioning"))
                with db:
                    db.execute("""UPDATE projects SET state='active'
                        WHERE owner_id=? AND project_id=? AND proposal_id=?""",
                        (proposal.owner_id, proposal.project_id, proposal.proposal_id))
                return workspace
            except ProjectLeaderDenied:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise ProjectLeaderUnavailable("project provisioning failed") from exc

    def get(self, scope: OwnerScope, project_id: str) -> ProjectWorkspace | None:
        """Inspect an approved project; never create one as a read side effect."""
        self._authorize(scope, project_id)
        with self._mutex:
            try:
                row = self._db().execute("""SELECT owner_id,project_id,title,
                    proposal_id,proposal_sha256,approval_channel,state
                    FROM projects WHERE owner_id=? AND project_id=?""",
                    (scope.owner_id, project_id)).fetchone()
                if row is None:
                    return None
                if row[6] != "active":
                    raise ProjectLeaderUnavailable("project provisioning incomplete")
                return self._ensure_project(tuple(row[:6]), create=False)
            except (OSError, sqlite3.Error) as exc:
                raise ProjectLeaderUnavailable("project workspace unavailable") from exc

    def append_reference(self, scope: OwnerScope, project_id: str, *, kind: str,
                         source_ref: str, checksum: str | None = None,
                         verified: bool = False) -> ManagementReference:
        """Minimal trusted metadata write; NOT a specialist/LLM write endpoint."""
        if (type(kind) is not str or kind not in KINDS
                or type(source_ref) is not str or not 1 <= len(source_ref) <= 2048
                or any(ord(c) < 32 or ord(c) == 127 for c in source_ref)
                or type(verified) is not bool
                or (checksum is not None and (type(checksum) is not str
                                              or not _SHA256.fullmatch(checksum)))
                or (verified and checksum is None)):
            raise InvalidProjectLeader("invalid management reference")
        with self._mutex:
            workspace = self.get(scope, project_id)
            if workspace is None:
                raise ProjectLeaderUnavailable("project is not approved")
            ref = ManagementReference("ref_" + uuid4().hex, kind, source_ref,
                                      checksum, verified)
            try:
                with closing(_database(workspace.database)) as db:
                    with db:
                        db.execute("INSERT INTO management_refs VALUES (?,?,?,?,?)",
                                   (ref.reference_id, ref.kind, ref.source_ref,
                                    ref.checksum, int(ref.verified)))
                return ref
            except sqlite3.Error as exc:
                raise ProjectLeaderUnavailable("cannot save management reference") from exc

    def references(self, scope: OwnerScope,
                   project_id: str) -> tuple[ManagementReference, ...]:
        with self._mutex:
            workspace = self.get(scope, project_id)
            if workspace is None:
                return ()
            try:
                with closing(_database(workspace.database)) as db:
                    rows = db.execute("""SELECT reference_id,kind,source_ref,
                        checksum,verified FROM management_refs ORDER BY rowid""").fetchall()
                return tuple(ManagementReference(a, b, c, d, bool(e))
                             for a, b, c, d, e in rows)
            except sqlite3.Error as exc:
                raise ProjectLeaderUnavailable("cannot read management references") from exc

    def close(self) -> None:
        with self._mutex:
            if self._registry is not None:
                self._registry.close()
                self._registry = None
            if self._registry_fd is not None:
                os.close(self._registry_fd)
                self._registry_fd = None
            if self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None

    def __enter__(self) -> ProjectLeaderStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
