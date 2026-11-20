"""One trusted task owner and private intake in a single managed Linux process.

The process is deliberately inert: it has no admission, task scheduling, model
credentials, grants or automatic worker execution. A future explicitly
authorized runner may reuse its *existing* inbox, never open a second writer.
"""

from __future__ import annotations

from contextlib import contextmanager
import argparse
import os
from pathlib import Path
import signal
from threading import Event
from typing import Iterator

from .diagnostic_host_preflight import inspect_preprovisioned_host
from .diagnostic_intake_service import (
    IntakeUnavailable, _private_config, serve_local_intake,
)
from .project_leader import ProjectLeaderStore
from .single_project_diagnostic_runner import (
    RunnerHold, SINGLE_SCOPE, SingleProjectDiagnosticRunner,
)
from .task_api import TaskInbox
from .task_assignment import WorkspaceRoot
from .task_permissions import TaskPermissionGate
from .task_storage import SQLiteTaskStore


# Immutable source of the already owner-registered *project workspace*, not
# the independent trusted runtime code installed by the owner-admin channel.
APPROVED_PROJECT_SOURCE = "741fc1a4e62b52922611e1e28d4805d727dffe4d"


class ManagedRuntimeHold(RuntimeError):
    """Do not launch: private host state or singleton ownership is uncertain."""


@contextmanager
def open_singleton(config: Path, *, expected_project_source: str = APPROVED_PROJECT_SOURCE
                   ) -> Iterator[tuple[SingleProjectDiagnosticRunner, Path]]:
    """Open the preapproved inbox only after non-mutating exact host checks.

    No approval, queue record, permission, socket directory or database is
    created by this entry point. The singleton task lock is retained for the
    entire lifetime of the socket. All resources close on failure/shutdown.
    """
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise ManagedRuntimeHold("linux_only")
    if (type(expected_project_source) is not str
            or len(expected_project_source) != 40
            or any(c not in "0123456789abcdef" for c in expected_project_source)):
        raise ManagedRuntimeHold("invalid_project_source_pin")

    readiness = inspect_preprovisioned_host(config)
    if not readiness.candidate_ready:
        raise ManagedRuntimeHold("private_host_not_ready")
    if readiness.source_commit != expected_project_source:
        raise ManagedRuntimeHold("approved_project_source_changed")
    doc = _private_config(config)
    if (doc["owner_id"] != SINGLE_SCOPE.owner_id
            or doc["project_ids"] != ["ai-agent-engine"]):
        raise ManagedRuntimeHold("approved_project_scope_changed")

    leaders = None
    inbox = None
    store = None
    runner = None
    try:
        leaders = ProjectLeaderStore(Path(doc["project_leader_root"]))
        store = SQLiteTaskStore(Path(doc["task_db"]))
        inbox = TaskInbox(store=store, workspace_root=WorkspaceRoot(doc["workspace_root"]))
        gate = TaskPermissionGate(inbox)  # zero grants, no task execution
        runner = SingleProjectDiagnosticRunner(
            inbox=inbox, scope=SINGLE_SCOPE, leaders=leaders,
            config=config, gate=gate,
        )
        yield runner, Path(doc["socket_path"])
    finally:
        if runner is not None:
            runner.close()
        if inbox is not None:
            inbox.close()
        elif store is not None:
            store.close()
        if leaders is not None:
            leaders.close()


def run_managed(config: Path, stopped: Event, *,
                expected_project_source: str = APPROVED_PROJECT_SOURCE) -> None:
    """Run one in-process intake; no worker loop runs behind this socket."""
    if not isinstance(stopped, Event):
        raise ManagedRuntimeHold("trusted_shutdown_required")
    with open_singleton(config, expected_project_source=expected_project_source) as (runner, path):
        serve_local_intake(runner.adapter, path, stopped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Managed one-project diagnostic runtime")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    stopped = Event()
    old_term = signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    old_int = signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        run_managed(args.config, stopped)
        return 0
    except (ManagedRuntimeHold, IntakeUnavailable, RunnerHold, ValueError, OSError,
            RuntimeError) as exc:
        # Never leak paths, task content, private registry state or credentials.
        print("managed diagnostics unavailable: " + type(exc).__name__, flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


if __name__ == "__main__":
    raise SystemExit(main())
