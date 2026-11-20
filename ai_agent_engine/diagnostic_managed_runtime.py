"""One trusted task owner and private intake in a single managed Linux process.

The normal process is deliberately inert: it has no admission, scheduling,
model credentials, grants or automatic worker execution. An independently
authenticated owner operator may supply one tightly scoped QA approval on
startup; the existing inbox and runner then execute it in the same process.
The standard CLI and private intake socket cannot supply such an approval.
"""

from __future__ import annotations

from contextlib import contextmanager
import argparse
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import signal
from threading import Event
import time
from typing import Iterator

from .diagnostic_host_preflight import inspect_preprovisioned_host
from .diagnostic_intake_service import (
    IntakeUnavailable, _private_config, serve_local_intake,
)
from .project_leader import ProjectLeaderStore
from .single_project_diagnostic_runner import (
    QA_REQUIRED_MARKER, RunnerHold, SINGLE_SCOPE, WORKER_ID,
    SingleProjectDiagnosticRunner,
)
from .task_api import OwnerApprovalProof, TaskInbox, TaskSubmission
from .task_assignment import WorkspaceRoot
from .task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from .task_state import TaskState
from .task_storage import SQLiteTaskStore
from .task_versions import _git


# Immutable source of the already owner-registered *project workspace*, not
# the independent trusted runtime code installed by the owner-admin channel.
APPROVED_PROJECT_SOURCE = "741fc1a4e62b52922611e1e28d4805d727dffe4d"


class ManagedRuntimeHold(RuntimeError):
    """Do not launch: private host state or singleton ownership is uncertain."""


QA_SUBMISSION = TaskSubmission(
    project_id="ai-agent-engine", title="7.7 owner-approved read-only Git QA",
    prompt="Run only the pinned git diff --check; no model, network or mutation.",
)
QA_NEGATIVE_SUBMISSION = TaskSubmission(
    project_id="ai-agent-engine", title="7.7 explicitly expected negative QA",
    prompt="Verify real failure and incident from fixed missing Git marker; no source edits.",
)
QA_PROPOSAL_SHA256 = sha256(
    b"AI Agent Engine 7.7: one durable, owner-approved, read-only git diff "
    b"task with one expiring EXECUTE_TOOL grant for diagnostic-7-7"
).hexdigest()
QA_NEGATIVE_PROPOSAL_SHA256 = sha256(
    b"AI Agent Engine 7.7: one durable, owner-approved, expected negative "
    b"Git marker check with one expiring EXECUTE_TOOL grant and test incident"
).hexdigest()


@dataclass(frozen=True, slots=True)
class OwnerConfirmedDiagnosticQA:
    """Proof *from a separate authenticated operator*, never model or socket.

    This receipt is intentionally not an authenticator by itself. Deployment
    requires a separately reviewed signed operator to verify owner intent.
    """

    approval: OwnerApprovalProof
    negative_check: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.approval, OwnerApprovalProof)
                or type(self.negative_check) is not bool
                or self.approval.channel != "message"
                or self.approval.proposal_sha256 != (
                    QA_NEGATIVE_PROPOSAL_SHA256 if self.negative_check
                    else QA_PROPOSAL_SHA256)):
            raise ManagedRuntimeHold("exact_owner_qa_proposal_required")


@contextmanager
def open_singleton(config: Path, *,
                   expected_project_source: str = APPROVED_PROJECT_SOURCE,
                   qa: OwnerConfirmedDiagnosticQA | None = None
                   ) -> Iterator[tuple[SingleProjectDiagnosticRunner, Path]]:
    """Open the preapproved inbox only after non-mutating exact host checks.

    By default, creates no approval, queue record, grant or directory.
    With a separately authenticated exact QA receipt, admits only one fixed
    test task and grants only one expiring read-only Git tool invocation.
    The singleton task lock is retained for the socket lifetime; all resources
    close on failure/shutdown.
    """
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise ManagedRuntimeHold("linux_only")
    if (type(expected_project_source) is not str
            or len(expected_project_source) != 40
            or any(c not in "0123456789abcdef" for c in expected_project_source)):
        raise ManagedRuntimeHold("invalid_project_source_pin")
    if qa is not None and not isinstance(qa, OwnerConfirmedDiagnosticQA):
        raise ManagedRuntimeHold("owner_qa_operator_required")

    readiness = inspect_preprovisioned_host(config)
    if not readiness.candidate_ready:
        raise ManagedRuntimeHold("private_host_not_ready")
    if readiness.source_commit != expected_project_source:
        raise ManagedRuntimeHold("approved_project_source_changed")
    doc = _private_config(config)
    if (doc["owner_id"] != SINGLE_SCOPE.owner_id
            or doc["project_ids"] != ["ai-agent-engine"]):
        raise ManagedRuntimeHold("approved_project_scope_changed")
    if qa is not None and qa.negative_check:
        # Reject an unexpectedly present marker BEFORE the first durable task
        # record is written; the runner rechecks this race before its claim.
        marker = _git(doc["source_bindings"]["ai-agent-engine"], (
            "ls-tree", "--name-only", readiness.source_commit,
            "--", QA_REQUIRED_MARKER,
        ))
        if marker.strip():
            raise ManagedRuntimeHold("negative_qa_marker_exists")

    leaders = None
    inbox = None
    store = None
    runner = None
    try:
        leaders = ProjectLeaderStore(Path(doc["project_leader_root"]))
        store = SQLiteTaskStore(Path(doc["task_db"]))
        inbox = TaskInbox(store=store, workspace_root=WorkspaceRoot(doc["workspace_root"]))
        grants: tuple[ActionGrant, ...] = ()
        if qa is not None:
            # A second entry, even while still QUEUED, is a new run request.
            # Never replay a previous owner QA or consume somebody else's work.
            if inbox.snapshot(SINGLE_SCOPE):
                raise ManagedRuntimeHold("owner_qa_requires_empty_task_inbox")
            record = inbox.accept(
                QA_NEGATIVE_SUBMISSION if qa.negative_check else QA_SUBMISSION,
                SINGLE_SCOPE,
                idempotency_key=qa.approval.proposal_id,
                owner_approval=qa.approval,
            )
            if inbox.state(record.task_id, SINGLE_SCOPE) is not TaskState.QUEUED:
                raise ManagedRuntimeHold("owner_qa_task_already_consumed")
            grants = (ActionGrant(
                record.task_id, WORKER_ID, ProtectedAction.EXECUTE_TOOL,
                expires_at=time.time() + 90,
            ),)
        gate = TaskPermissionGate(inbox, grants=grants)
        runner = SingleProjectDiagnosticRunner(
            inbox=inbox, scope=SINGLE_SCOPE, leaders=leaders,
            config=config, gate=gate,
            owner_qa_negative_check=qa.negative_check if qa is not None else False,
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
                expected_project_source: str = APPROVED_PROJECT_SOURCE,
                qa: OwnerConfirmedDiagnosticQA | None = None) -> None:
    """Run intake, with at most one separately owner-confirmed QA on startup.

    No background worker loop exists. A genuine nonzero tool exit records a
    test incident via the existing diagnostic adapter; absent real failure,
    no incident is manufactured.
    """
    if not isinstance(stopped, Event):
        raise ManagedRuntimeHold("trusted_shutdown_required")
    with open_singleton(config, expected_project_source=expected_project_source,
                        qa=qa) as (runner, path):
        if qa is not None:
            outcome = runner.run_next()
            expected = TaskState.FAILED if qa.negative_check else TaskState.SUCCEEDED
            if (outcome is None or outcome.state is not expected
                    or (qa.negative_check and outcome.incident_id is None)
                    or (not qa.negative_check and outcome.incident_id is not None)):
                raise ManagedRuntimeHold("owner_qa_outcome_unverified")
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
