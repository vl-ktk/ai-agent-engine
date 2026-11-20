"""Stage 7.7: opt-in, single-project, one-shot offline task executor.

This is a trusted owner-process component, not a daemon/HTTP endpoint. It
consumes only pre-admitted tasks; their prompts never become shell arguments,
grants, model calls, credentials or project selection. Netcup is not enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from threading import Lock
from weakref import WeakSet

from .diagnostic_intake_service import IntakeUnavailable, bind_inprocess_diagnostics
from .project_diagnostics import FailureSignal, DiagnosticHold, capture_git_source
from .project_leader import ProjectLeaderStore
from .task_api import OwnerScope, TaskInbox
from .task_control import TaskControlError, TaskProcessController
from .task_delivery import DeliveryRejected, TaskDeliveryManager
from .task_permissions import (
    PermissionDenied, PermissionUnavailable, ProtectedAction, TaskPermissionGate,
)
from .task_state import TaskState
from .task_versions import _git, SourceVersionRejected


SINGLE_SCOPE = OwnerScope("vl-ktk", frozenset({"ai-agent-engine"}))
WORKER_ID = "diagnostic-7-7"
COMMAND_ID = "git_diff_check"
RUN_TIMEOUT = 15
_REGISTRY_LOCK = Lock()
_BOUND_INBOXES: WeakSet[TaskInbox] = WeakSet()


class RunnerHold(RuntimeError):
    """Blocked or unverifiable task execution; never an implicit retry."""


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    task_id: str
    state: TaskState
    incident_id: str | None


def _trusted_git_check() -> tuple[str, ...]:
    git = shutil.which("git", path="/usr/bin:/bin")
    if git is None:
        raise RunnerHold("trusted_git_unavailable")
    return (git, "--no-pager", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null", "-c", "diff.external=",
            "-c", "submodule.recurse=false", "diff", "--no-ext-diff",
            "--check", "--")


def _isolated_worker_env() -> dict[str, str]:
    """Host child has no inherited credentials or provider configuration."""
    return {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }


class SingleProjectDiagnosticRunner:
    """Execute at most one approved scoped Git check per explicit call.

    Requires the *already opened* exclusive TaskInbox, private configuration
    and approved leader registry; creates no owner approval, task, second DB
    owner, grants, socket or service. A caller must authorize EXECUTE_TOOL for
    the chosen task and worker. Failed attempts are never automatically retried.
    """

    def __init__(self, *, inbox: TaskInbox, scope: OwnerScope,
                 leaders: ProjectLeaderStore, config: Path,
                 gate: TaskPermissionGate) -> None:
        if (scope != SINGLE_SCOPE or not isinstance(gate, TaskPermissionGate)
                or gate.inbox is not inbox):
            raise RunnerHold("single_approved_project_and_grants_required")
        try:
            adapter, _socket_path = bind_inprocess_diagnostics(
                config, inbox=inbox, scope=scope, leaders=leaders)
        except (DiagnosticHold, IntakeUnavailable, ValueError, OSError) as exc:
            raise RunnerHold("approved_project_runtime_required") from exc
        self.inbox = inbox
        self.scope = scope
        self.gate = gate
        self.adapter = adapter
        self.delivery = TaskDeliveryManager(inbox)
        self.controller = TaskProcessController(inbox, permission_gate=gate)
        self._run_lock = Lock()
        self.source = Path(adapter._diagnostics._bindings[(scope.owner_id,
                                                           "ai-agent-engine")])
        with _REGISTRY_LOCK:
            if inbox in _BOUND_INBOXES:
                raise RunnerHold("one_runner_per_task_inbox_required")
            _BOUND_INBOXES.add(inbox)

    def run_next(self) -> RunnerOutcome | None:
        """Serialize one explicit task operation; no concurrent dispatch."""
        if not self._run_lock.acquire(blocking=False):
            raise RunnerHold("runner_already_active")
        try:
            return self._run_next_locked()
        finally:
            self._run_lock.release()

    def _run_next_locked(self) -> RunnerOutcome | None:
        """Claim one eligible queued task, run fixed read-only Git check.

        No automatic retry/resume: any preexisting ASSIGNED/RUNNING work must
        be inspected and cleared by the trusted owner before new execution.
        """
        if any(item.review_required for item in self.delivery.recovery_inventory(self.scope)):
            raise RunnerHold("unfinished_task_requires_owner_review")
        choice = self.inbox.select_next("ai-agent-engine", self.scope)
        if choice is None:
            return None
        task_id = choice.task_id
        command = _trusted_git_check()
        # Observe a trustworthy pre-execution commit and source identity
        # before claiming or launching anything. Task prompts are not read.
        try:
            baseline = _git(str(self.source),
                            ("rev-parse", "--verify", "HEAD^{commit}")).decode("ascii").strip()
            pinned_source = capture_git_source(self.source, baseline)
        except (SourceVersionRejected, DiagnosticHold, OSError, UnicodeError) as exc:
            raise RunnerHold("source_unverifiable") from exc
        self.inbox.claim_task(task_id, WORKER_ID, self.source.name, self.scope)
        delivery = self.delivery.reserve(task_id, self.scope,
                                         actor_id=WORKER_ID, max_attempts=1)
        if not delivery.should_execute or delivery.token is None:
            raise RunnerHold("unsafe_duplicate_delivery")

        try:
            child = self.controller.launch_gated(
                task_id, self.scope, actor_id=WORKER_ID,
                worker_argv=command, cwd=str(self.source),
                environment=_isolated_worker_env(),
                timeout_seconds=RUN_TIMEOUT, quiet=True,
                before_release=lambda: self.gate.run(
                    task_id, self.scope, actor_id=WORKER_ID,
                    action=ProtectedAction.EXECUTE_TOOL,
                    operation=lambda: None))
            child.wait(timeout=RUN_TIMEOUT + 3)
            if self.inbox.state(task_id, self.scope) is not TaskState.RUNNING:
                raise RunnerHold("managed_process_not_running")
            if (child.returncode is None or child.returncode < 0
                    or capture_git_source(self.source, baseline) != pinned_source):
                raise RunnerHold("source_or_process_changed")
            if child.returncode == 0:
                self.controller.finish(task_id, self.scope, success=True)
                self.delivery.report(task_id, self.scope, actor_id=WORKER_ID,
                                     token=delivery.token, success=True)
                return RunnerOutcome(task_id, TaskState.SUCCEEDED, None)

            # Only a verified child exit can become a terminal tool failure.
            self.controller.complete_failed_attempt(task_id, self.scope)
            ack = self.delivery.report(task_id, self.scope, actor_id=WORKER_ID,
                                       token=delivery.token, success=False)
            if ack.status != "exhausted":
                raise RunnerHold("unexpected_delivery_retry_state")
            self.controller.finalize_exhausted(task_id, self.scope)
            signal = FailureSignal(task_id, "tool_failure", "GitDiffCheckFailed",
                                   COMMAND_ID, "exit 0", "nonzero exit",
                                   "Trusted read-only Git diff check failed")
            incident = self.adapter.record_failed_task(
                task_id, signal=signal, baseline_commit=baseline)
            return RunnerOutcome(task_id, TaskState.FAILED, incident.incident_id)
        except (subprocess.TimeoutExpired, TaskControlError, DiagnosticHold, RunnerHold,
                DeliveryRejected, PermissionDenied, PermissionUnavailable,
                OSError, ValueError) as exc:
            # A failed diagnostic write may follow an already-committed FAILED
            # task. Never reissue its delivery token or launch another child.
            self.close()
            raise RunnerHold("execution_requires_owner_review") from exc

    def close(self) -> None:
        """Stop owned subprocesses; caller retains task inbox and registry."""
        self.controller.close()
        with _REGISTRY_LOCK:
            _BOUND_INBOXES.discard(self.inbox)
