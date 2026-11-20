"""Stage 7.7: trusted, opt-in TaskInbox-to-project-diagnostics intake.

This is a callable boundary for a future authenticated task runner, not a
background poller, model instruction, HTTP endpoint, or deploy permission.
Only an accepted durable FAILED transition from the claimed worker can be
recorded. The assigned checkout must also be the approved project Git source.
"""

from __future__ import annotations

from .project_diagnostics import (
    DiagnosticHold, FailureSignal, Incident, ProjectDiagnosticsStore,
    capture_git_source,
)
from .task_control import TaskProcessController
from .task_api import OwnerScope, TaskInbox
from .task_state import TaskState


class TaskDiagnosticAdapter:
    """Bind a durable task transition to one source-verified incident.

    The caller must be a trusted host process and supply a pre-execution
    baseline SHA. No untrusted model text may choose the scope or source path.
    """

    def __init__(self, inbox: TaskInbox, scope: OwnerScope,
                 diagnostics: ProjectDiagnosticsStore) -> None:
        if (not isinstance(inbox, TaskInbox) or not inbox.durable
                or not isinstance(scope, OwnerScope)
                or not isinstance(diagnostics, ProjectDiagnosticsStore)):
            raise DiagnosticHold("trusted_durable_task_context_required")
        self._inbox = inbox
        self._scope = scope
        self._diagnostics = diagnostics

    def record_failed_task(self, task_id: str, *,
                           signal: FailureSignal,
                           baseline_commit: str) -> Incident:
        """Record one proven task failure; never launch a repair or model.

        Idempotency is tied to the *durably accepted* task transition ID, not
        to a key supplied by the executor. A conflicting replay is held.
        """
        record = self._inbox.get(task_id, self._scope)
        if record is None:
            raise DiagnosticHold("task_unavailable")
        if not isinstance(signal, FailureSignal) or signal.task_id != task_id:
            raise DiagnosticHold("failure_task_mismatch")
        assignment = self._inbox.get_assignment(task_id, self._scope)
        if (assignment is None
                or self._inbox.state(task_id, self._scope) is not TaskState.FAILED):
            raise DiagnosticHold("verified_failure_required")
        history = self._inbox.transition_history(task_id, self._scope)
        accepted = [event for event in history if
                    event.accepted and event.to_state is TaskState.FAILED]
        if (len(accepted) != 1
                or accepted[0].actor_id != assignment.worker_id
                or accepted[0].reason_code != "transition"):
            raise DiagnosticHold("trusted_worker_failure_required")

        # An assignment path is from WorkspaceRoot, not executor/model text.
        # ProjectDiagnosticsStore independently checks the *same* absolute
        # directory against its owner/project binding and re-verifies Git.
        source = capture_git_source(assignment.workspace_dir, baseline_commit)
        return self._diagnostics.record_failure(
            self._scope, record.project_id, signal=signal, source=source,
            delivery_key=f"taskfail_{task_id}_{accepted[0].event_id}",
        )

    def finish_managed_process(self, controller: TaskProcessController,
                               task_id: str, *, success: bool,
                               baseline_commit: str | None = None,
                               signal: FailureSignal | None = None) -> Incident | None:
        """Opt-in completion of a real managed child, then diagnostic intake.

        The process controller proves that its *entire* process group exited
        and that success matches the actual return code. Failure is committed
        in TaskInbox before a separate diagnostic SQLite transaction. A failed
        diagnostic write therefore leaves the task FAILED (never re-executes
        it); the trusted owner can reconcile it with record_failed_task.

        baseline_commit must be retained by the trusted caller from before
        process execution. This is not a public endpoint or an auto-runner.
        """
        if (not isinstance(controller, TaskProcessController)
                or controller.inbox is not self._inbox):
            raise DiagnosticHold("managed_process_owner_mismatch")
        if type(success) is not bool:
            raise DiagnosticHold("invalid_managed_result")
        if success:
            if signal is not None or baseline_commit is not None:
                raise DiagnosticHold("unexpected_diagnostic_on_success")
        else:
            if (not isinstance(signal, FailureSignal)
                    or signal.task_id != task_id
                    or not isinstance(baseline_commit, str)):
                raise DiagnosticHold("trusted_failure_evidence_required")
            record = self._inbox.get(task_id, self._scope)
            assignment = self._inbox.get_assignment(task_id, self._scope)
            if record is None or assignment is None:
                raise DiagnosticHold("trusted_assignment_required")
            # Before making a durable terminal transition, refuse a foreign
            # checkout or unverifiable Git baseline. Rechecked on intake.
            source = capture_git_source(assignment.workspace_dir, baseline_commit)
            self._diagnostics._bound(self._scope, record.project_id, source)

        result = controller.finish(task_id, self._scope, success=success)
        if result is TaskState.SUCCEEDED:
            return None
        return self.record_failed_task(task_id, signal=signal,
                                       baseline_commit=baseline_commit)

    def recover_project_unknown(self, project_id: str) -> tuple[str, ...]:
        """Explicit fail-closed reconciliation; no restart or auto-replay."""
        if project_id not in self._scope.project_ids:
            raise DiagnosticHold("project_outside_scope")
        return self._diagnostics.recover_unknown(self._scope, project_id)
