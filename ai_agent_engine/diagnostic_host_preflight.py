"""Non-mutating, one-project host-readiness inspection for stage 7.7.

Never opens a writable SQLite store, provisions owner approvals or starts
tasks. This is a local offline/host *candidate* check, not a live deployment
gate or a substitute for the owner-controlled GitHub release workflows.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterator

from .diagnostic_intake_service import IntakeUnavailable, _private_config
from .project_diagnostics import DiagnosticHold, capture_git_source
from .project_leader import (
    PROJECT_APPLICATION_ID, PROJECT_SCHEMA_VERSION,
    REGISTRY_APPLICATION_ID, SCHEMA_VERSION as REGISTRY_SCHEMA_VERSION,
)
from .single_project_diagnostic_runner import SINGLE_SCOPE
from .task_storage import APPLICATION_ID as TASK_APPLICATION_ID, SCHEMA_VERSION as TASK_SCHEMA_VERSION
from .task_versions import SourceVersionRejected, _git


class PreflightHold(RuntimeError):
    """A private host component is absent, busy or inconsistent."""


@dataclass(frozen=True, slots=True)
class HostReadiness:
    candidate_ready: bool
    blockers: tuple[str, ...]
    checked: tuple[str, ...]
    source_commit: str | None = None


def _private_directory(path: Path) -> None:
    if (not path.is_absolute() or str(path) != os.path.realpath(path)):
        raise PreflightHold("unsafe_private_directory")
    try:
        info = path.lstat()
    except OSError:
        raise PreflightHold("missing_private_directory") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise PreflightHold("unsafe_private_directory")


@contextmanager
def _readonly_sqlite(path: Path, app: int, version: int) -> Iterator[sqlite3.Connection]:
    """Reject live locks, journals, symlinks and schema mismatch; no writes.

    `immutable=1` is safe here only after ruling out active writers and WAL.
    SQLite must not create -shm/-wal or migrate an old private database.
    """
    _private_directory(path.parent)
    if not path.is_absolute() or str(path) != os.path.realpath(path):
        raise PreflightHold("unsafe_database_file")
    if any(p.exists() or p.is_symlink() for p in
           (Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        raise PreflightHold("database_has_pending_journal")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise PreflightHold("unsafe_database_file")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PreflightHold("database_owned_by_running_process") from None
        # Connect to the exact verified inode, not a re-resolved pathname.
        target = f"file:/proc/self/fd/{fd}?mode=ro&immutable=1"
        with closing(sqlite3.connect(target, uri=True, timeout=0)) as db:
            db.execute("PRAGMA query_only=ON")
            actual = path.stat(follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise PreflightHold("database_identity_changed")
            recorded = (db.execute("PRAGMA application_id").fetchone()[0],
                        db.execute("PRAGMA user_version").fetchone()[0])
            if recorded != (app, version):
                raise PreflightHold("database_schema_mismatch")
            yield db
    except (FileNotFoundError, PermissionError, sqlite3.Error, OSError):
        raise PreflightHold("database_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def inspect_preprovisioned_host(config: Path) -> HostReadiness:
    """Inspect only an *already prepared* host, without side effects.

    Every blocker is a bounded code; private paths, task prompts, approval
    proofs and database contents are never returned. Even an all-PASS report
    is not an authenticated owner approval or a production release permit.
    """
    blockers: list[str] = []
    checked: list[str] = []
    head: str | None = None

    def check(label: str, action):
        try:
            result = action()
        except (PreflightHold, DiagnosticHold, SourceVersionRejected, OSError,
                ValueError, sqlite3.Error):
            blockers.append(label)
            return None
        checked.append(label)
        return result

    try:
        doc = _private_config(Path(config))
    except (IntakeUnavailable, OSError, ValueError, TypeError):
        return HostReadiness(False, ("private_config",), ())
    if (doc["owner_id"] != SINGLE_SCOPE.owner_id
            or doc["project_ids"] != ["ai-agent-engine"]):
        return HostReadiness(False, ("single_project_identity",), ("private_config",))
    checked.append("private_config")

    project_root = Path(doc["project_leader_root"])
    workspace_root = Path(doc["workspace_root"])
    task_db = Path(doc["task_db"])
    socket_path = Path(doc["socket_path"])
    source = Path(doc["source_bindings"]["ai-agent-engine"])

    def check_directories():
        for directory in (project_root, project_root / "vl-ktk",
                          project_root / "vl-ktk" / "ai-agent-engine",
                          project_root / "vl-ktk" / "ai-agent-engine" / "documents",
                          workspace_root, source, socket_path.parent):
            _private_directory(directory)
        if (source.parent != workspace_root or not socket_path.is_absolute()
                or str(socket_path) != os.path.realpath(socket_path)
                or socket_path.exists() or socket_path.is_symlink()):
            raise PreflightHold("unexpected_runtime_layout")

    check("private_host_directories", check_directories)

    registry = project_root / "project-leaders.sqlite3"
    leader = project_root / "vl-ktk" / "ai-agent-engine" / "leader.sqlite3"

    def check_approval():
        with _readonly_sqlite(registry, REGISTRY_APPLICATION_ID,
                              REGISTRY_SCHEMA_VERSION) as db:
            row = db.execute("""SELECT owner_id,project_id,title,proposal_id,
                proposal_sha256,approval_channel,state FROM projects
                WHERE owner_id=? AND project_id=?""",
                ("vl-ktk", "ai-agent-engine")).fetchone()
            if row is None or row[6] != "active":
                raise PreflightHold("owner_approval_missing")
            identity = row[:6]
        with _readonly_sqlite(leader, PROJECT_APPLICATION_ID,
                              PROJECT_SCHEMA_VERSION) as db:
            actual = db.execute("""SELECT owner_id,project_id,title,proposal_id,
                proposal_sha256,approval_channel FROM identity
                WHERE singleton=1""").fetchone()
            if actual != identity:
                raise PreflightHold("project_approval_identity_mismatch")

    check("existing_project_approval", check_approval)

    def check_task_db():
        with _readonly_sqlite(task_db, TASK_APPLICATION_ID,
                              TASK_SCHEMA_VERSION) as db:
            foreign = db.execute("""SELECT COUNT(*) FROM tasks
                WHERE owner_id<>? OR project_id<>?""",
                ("vl-ktk", "ai-agent-engine")).fetchone()[0]
            active = db.execute("""SELECT COUNT(*) FROM task_states
                WHERE state IN ('assigned','running')""").fetchone()[0]
            pending = db.execute("""SELECT COUNT(*) FROM task_deliveries
                WHERE status IN ('active','retryable')""").fetchone()[0]
            if foreign or active or pending:
                raise PreflightHold("foreign_or_unresolved_task_state")

    check("isolated_inactive_task_db", check_task_db)

    def check_source():
        raw = _git(str(source), ("rev-parse", "--verify", "HEAD^{commit}"))
        commit = raw.decode("ascii").strip()
        verified = capture_git_source(source, commit)
        if verified.patch_sha256 is not None:
            raise PreflightHold("dirty_host_source")
        return commit

    head = check("clean_verified_git_source", check_source)
    return HostReadiness(not blockers, tuple(blockers), tuple(checked), head)


def main() -> int:
    """Read-only operator hook; print only bounded status codes and Git SHA."""
    parser = argparse.ArgumentParser(description="7.7 private host readiness (no writes)")
    parser.add_argument("--config", required=True, type=Path)
    options = parser.parse_args()
    report = inspect_preprovisioned_host(options.config)
    print(json.dumps({"candidate_ready": report.candidate_ready,
                      "blockers": report.blockers, "checked": report.checked,
                      "source_commit": report.source_commit}, separators=(",", ":")))
    return 0 if report.candidate_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
