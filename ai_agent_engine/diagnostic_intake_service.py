"""Opt-in Linux private-socket intake for verified stage-7.7 task failures.

This service has no model adapter, task admission, HTTP listener, shell access,
deployment action, automatic repair or privilege grant. It is intended to run
inside the *same trusted task runtime* that owns the durable TaskInbox; do not
start a second independent writer against an active TaskInbox database.
"""

from __future__ import annotations

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
from .task_assignment import InvalidAssignment, WorkspaceRoot
from .task_storage import StorageUnavailable


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
            or len(doc["project_ids"]) != 1
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


def bind_inprocess_diagnostics(config: Path, *, inbox: TaskInbox,
                               scope: OwnerScope,
                               leaders: ProjectLeaderStore) -> tuple[TaskDiagnosticAdapter, Path]:
    """Bind diagnostics to the *existing* exclusive TaskInbox and registry.

    This function never opens another task store, creates an approval, or
    executes a task. Only the trusted task-process owner may pass the live
    inbox and its already-approved project registry. Socket intake and direct
    adapter calls then share that same process-local durable task state.
    """
    if (not isinstance(inbox, TaskInbox) or not inbox.durable
            or not isinstance(scope, OwnerScope)
            or not isinstance(leaders, ProjectLeaderStore)
            or inbox._store is None or inbox._store._fd is None
            or inbox._store._connection is None
            or not isinstance(inbox._workspace_root, WorkspaceRoot)
            or leaders._root_fd is None or leaders._registry is None):
        raise IntakeUnavailable("existing_task_owner_and_registry_required")
    doc = _private_config(config)
    if scope != OwnerScope(doc["owner_id"], frozenset(doc["project_ids"])):
        raise IntakeUnavailable("owner_project_scope_mismatch")
    task_path = Path(doc["task_db"])
    workspace_root = Path(doc["workspace_root"])
    project_root = Path(doc["project_leader_root"])
    if (not task_path.is_absolute() or str(task_path) != os.path.realpath(task_path)
            or task_path != inbox._store.path
            or not workspace_root.is_absolute()
            or workspace_root != inbox._workspace_root.path
            or str(workspace_root) != os.path.realpath(workspace_root)
            or not project_root.is_absolute() or project_root != leaders.root
            or str(project_root) != os.path.realpath(project_root)):
        raise IntakeUnavailable("existing_runtime_binding_mismatch")
    try:
        actual = task_path.stat(follow_symlinks=False)
        open_fd = os.fstat(inbox._store._fd)
        if (not stat.S_ISREG(actual.st_mode)
                or actual.st_uid != os.geteuid()
                or stat.S_IMODE(actual.st_mode) != 0o600
                or (actual.st_dev, actual.st_ino) != (open_fd.st_dev, open_fd.st_ino)):
            raise IntakeUnavailable("task_database_identity_changed")
    except OSError:
        raise IntakeUnavailable("task_database_identity_changed") from None

    bindings: dict[tuple[str, str], Path] = {}
    for project, source in doc["source_bindings"].items():
        directory = Path(source)
        if (not directory.is_absolute() or directory.parent != workspace_root
                or str(directory) != os.path.realpath(directory)):
            raise IntakeUnavailable("source_outside_trusted_workspace")
        try:
            if inbox._workspace_root.resolve(directory.name) != str(directory):
                raise IntakeUnavailable("source_outside_trusted_workspace")
        except (InvalidAssignment, OSError, ValueError):
            raise IntakeUnavailable("source_outside_trusted_workspace") from None
        if leaders.get(scope, project) is None:
            raise IntakeUnavailable("approved_project_required")
        bindings[(scope.owner_id, project)] = directory

    diagnostics = ProjectDiagnosticsStore(leaders, source_bindings=bindings)
    adapter = TaskDiagnosticAdapter(inbox, scope, diagnostics)
    # Crash uncertainty stays HOLD. Never autonomously rerun failed work.
    for project in scope.project_ids:
        adapter.recover_project_unknown(project)
    return adapter, Path(doc["socket_path"])


def run_configured_service(config: Path, stopped: Event, *,
                           inbox: TaskInbox | None = None,
                           scope: OwnerScope | None = None,
                           leaders: ProjectLeaderStore | None = None) -> None:
    """Serve intake *inside* its owning task process, never as a sidecar.

    The standalone CLI intentionally has no live inbox and is refused. A host
    task runtime must supply all three trusted objects explicitly; their
    ownership remains with the caller after this method returns.
    """
    if inbox is None or scope is None or leaders is None:
        raise IntakeUnavailable("standalone_task_owner_forbidden")
    if not isinstance(stopped, Event):
        raise IntakeUnavailable("trusted_shutdown_event_required")
    adapter, socket_path = bind_inprocess_diagnostics(
        config, inbox=inbox, scope=scope, leaders=leaders)
    serve_local_intake(adapter, socket_path, stopped)


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
