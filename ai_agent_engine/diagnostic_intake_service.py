"""Opt-in Linux private-socket intake for verified stage-7.7 task failures.

This service has no model adapter, task admission, HTTP listener, shell access,
deployment action, automatic repair or privilege grant. It is intended to run
inside the *same trusted task runtime* that owns the durable TaskInbox; do not
start a second independent writer against an active TaskInbox database.
"""

from __future__ import annotations

from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import struct
from threading import Event

from .project_diagnostic_adapter import TaskDiagnosticAdapter
from .project_diagnostics import (
    DiagnosticHold, FailureSignal, InvalidDiagnostic, ProjectDiagnosticsStore,
)
from .project_leader import ProjectLeaderStore, ProjectLeaderUnavailable
from .task_api import OwnerScope, ProjectForbidden, TaskInbox
from .task_assignment import WorkspaceRoot
from .task_storage import (
    APPLICATION_ID as TASK_APP_ID, SCHEMA_VERSION as TASK_SCHEMA,
    SQLiteTaskStore, StorageUnavailable,
)


MAX_EVENT_BYTES = 2048
MAX_BACKLOG = 8


class IntakeUnavailable(RuntimeError):
    """The local private intake cannot safely accept events."""


def _private_socket_parent(path: Path) -> None:
    if (not path.is_absolute() or path.name in ("", ".", "..")
            or str(path) != os.path.realpath(path)):
        raise IntakeUnavailable("unsafe_socket_path")
    try:
        directory = path.parent.stat(follow_symlinks=False)
    except OSError:
        raise IntakeUnavailable("missing_private_runtime_directory") from None
    if (not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or stat.S_IMODE(directory.st_mode) != 0o700):
        raise IntakeUnavailable("unsafe_runtime_directory")
    if path.exists() or path.is_symlink():
        raise IntakeUnavailable("socket_path_occupied")


def _decode_event(raw: bytes) -> tuple[str, str, FailureSignal]:
    if (type(raw) is not bytes or not 1 <= len(raw) <= MAX_EVENT_BYTES
            or not raw.endswith(b"\n")):
        raise InvalidDiagnostic("invalid_event_size")

    def no_dupes(pairs: list[tuple[str, object]]) -> dict[str, object]:
        values: dict[str, object] = {}
        for name, value in pairs:
            if name in values:
                raise InvalidDiagnostic("duplicate_event_field")
            values[name] = value
        return values

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_dupes)
    except (UnicodeError, ValueError):
        raise InvalidDiagnostic("invalid_event_json") from None
    if (type(data) is not dict
            or set(data) != {"schema", "task_id", "baseline_commit", "failure"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or type(data["failure"]) is not dict):
        raise InvalidDiagnostic("invalid_event_envelope")
    failure = data["failure"]
    fields = {"kind", "error_code", "check_name", "expected", "actual", "excerpt"}
    if set(failure) != fields or any(type(failure[k]) is not str for k in fields):
        raise InvalidDiagnostic("invalid_failure_fields")
    if (type(data["task_id"]) is not str
            or type(data["baseline_commit"]) is not str):
        raise InvalidDiagnostic("invalid_failure_identity")
    return data["task_id"], data["baseline_commit"], FailureSignal(
        data["task_id"], failure["kind"], failure["error_code"],
        failure["check_name"], failure["expected"], failure["actual"],
        failure["excerpt"],
    )


def process_local_event(adapter: TaskDiagnosticAdapter, raw: bytes) -> dict[str, object]:
    """One bounded request; only accepted durable failures create evidence."""
    if not isinstance(adapter, TaskDiagnosticAdapter):
        raise IntakeUnavailable("trusted_adapter_required")
    try:
        task_id, baseline, signal = _decode_event(raw)
        incident = adapter.record_failed_task(
            task_id, baseline_commit=baseline, signal=signal,
        )
    except (InvalidDiagnostic, DiagnosticHold, ProjectLeaderUnavailable,
            ProjectForbidden, StorageUnavailable, sqlite3.Error,
            OSError, ValueError):
        return {"ok": False, "status": "HOLD"}
    return {"ok": True, "status": "RECORDED", "incident_id": incident.incident_id}


def serve_local_intake(adapter: TaskDiagnosticAdapter, path: Path,
                       stopped: Event) -> None:
    """Serial single-UID AF_UNIX service, zero network or model execution.

    A request is one newline-terminated JSON object, max 2048 bytes. Socket
    is 0600 under an already-created private 0700 directory. Missing owner
    project approvals fail inside the adapter; this service never creates them.
    """
    if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
        raise IntakeUnavailable("linux_peer_credentials_required")
    if not isinstance(stopped, Event):
        raise IntakeUnavailable("trusted_shutdown_event_required")
    path = Path(path)
    _private_socket_parent(path)
    prior_umask = os.umask(0o077)
    inode: int | None = None
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            inode = path.stat(follow_symlinks=False).st_ino
            server.listen(MAX_BACKLOG)
            server.settimeout(0.25)
            while not stopped.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with closing(conn):
                    conn.settimeout(2.0)
                    try:
                        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        _pid, uid, _gid = struct.unpack("3i", creds)
                        if uid != os.geteuid():
                            continue
                        with conn.makefile("rb", buffering=0) as source:
                            raw = source.readline(MAX_EVENT_BYTES + 1)
                        answer = process_local_event(adapter, raw)
                        conn.sendall(json.dumps(answer, separators=(",", ":")).encode() + b"\n")
                    except (TimeoutError, OSError, ValueError):
                        # Never print raw event data or disclose project scope.
                        continue
        finally:
            server.close()
            try:
                current = path.stat(follow_symlinks=False)
                if inode is not None and stat.S_ISSOCK(current.st_mode) and current.st_ino == inode:
                    path.unlink()
            except OSError:
                pass
    finally:
        os.umask(prior_umask)


def _private_config(path: Path) -> dict[str, object]:
    """Read a locally provisioned owner-approved configuration, never a task."""
    if not path.is_absolute() or str(path) != os.path.realpath(path):
        raise IntakeUnavailable("unsafe_config_path")
    _private_socket_parent(path.with_name(path.name + ".unused-socket-check"))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > 4096):
                raise IntakeUnavailable("unsafe_config_file")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        def no_dupes(pairs: list[tuple[str, object]]) -> dict[str, object]:
            obj: dict[str, object] = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("duplicate config field")
                obj[key] = value
            return obj

        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=no_dupes)
    except (OSError, UnicodeError, ValueError):
        raise IntakeUnavailable("invalid_private_config") from None
    if (type(doc) is not dict or set(doc) != {
            "schema", "owner_id", "project_ids", "project_leader_root",
            "workspace_root", "task_db", "socket_path", "source_bindings",
    } or doc["schema"] != 1 or type(doc["schema"]) is not int
            or type(doc["project_ids"]) is not list
            or not doc["project_ids"] or len(doc["project_ids"]) > 16
            or any(type(p) is not str for p in doc["project_ids"])
            or len(set(doc["project_ids"])) != len(doc["project_ids"])
            or type(doc["source_bindings"]) is not dict
            or set(doc["source_bindings"]) != set(doc["project_ids"])):
        raise IntakeUnavailable("invalid_private_config")
    if any(type(doc[field]) is not str for field in (
            "owner_id", "project_leader_root", "workspace_root",
            "task_db", "socket_path")):
        raise IntakeUnavailable("invalid_private_config")
    if any(type(p) is not str for p in doc["source_bindings"].values()):
        raise IntakeUnavailable("invalid_private_config")
    return doc


def run_configured_service(config: Path, stopped: Event) -> None:
    """Own one durable TaskInbox exclusively; never authenticate an owner itself.

    A trusted host operator must provision both the pre-approved project
    registry and private config. No auto-creation of owner approvals or tasks.
    """
    doc = _private_config(config)
    scope = OwnerScope(doc["owner_id"], frozenset(doc["project_ids"]))
    task_path = Path(doc["task_db"])
    if (not task_path.is_absolute() or task_path.is_symlink()
            or not task_path.is_file()
            or task_path.stat().st_uid != os.geteuid()
            or stat.S_IMODE(task_path.stat().st_mode) != 0o600):
        raise IntakeUnavailable("preexisting_private_task_db_required")
    # Never silently migrate production task state as a side effect of starting
    # diagnostics. The owner must separately prepare/verify the exact schema.
    try:
        with closing(sqlite3.connect(f"file:{task_path}?mode=ro", uri=True)) as probe:
            if (probe.execute("PRAGMA application_id").fetchone()[0] != TASK_APP_ID
                    or probe.execute("PRAGMA user_version").fetchone()[0] != TASK_SCHEMA
                    or probe.execute("PRAGMA quick_check").fetchone()[0] != "ok"):
                raise IntakeUnavailable("task_db_not_prepared")
    except sqlite3.Error:
        raise IntakeUnavailable("task_db_not_prepared") from None
    bindings = {(scope.owner_id, project): Path(source) for project, source
                in doc["source_bindings"].items()}
    with ProjectLeaderStore(Path(doc["project_leader_root"])) as leaders:
        for project in scope.project_ids:
            if leaders.get(scope, project) is None:
                raise IntakeUnavailable("approved_project_required")
        inbox = TaskInbox(store=SQLiteTaskStore(task_path),
                          workspace_root=WorkspaceRoot(Path(doc["workspace_root"])))
        try:
            diagnostics = ProjectDiagnosticsStore(leaders, source_bindings=bindings)
            adapter = TaskDiagnosticAdapter(inbox, scope, diagnostics)
            # An uncertain interrupted repair is held. Never replay work.
            for project in scope.project_ids:
                adapter.recover_project_unknown(project)
            serve_local_intake(adapter, Path(doc["socket_path"]), stopped)
        finally:
            inbox.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private stage-7.7 diagnostic intake; no models")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    stopped = Event()
    previous_term = signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    previous_int = signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        run_configured_service(args.config, stopped)
    except (IntakeUnavailable, DiagnosticHold, ProjectLeaderUnavailable,
            ProjectForbidden, StorageUnavailable, sqlite3.Error,
            OSError, ValueError) as exc:
        # No private paths, evidence or tokens in the service journal.
        print(f"diagnostic intake unavailable: {type(exc).__name__}")
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
