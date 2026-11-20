"""Stage 5.13: fail-closed action grants and metadata-only durable audit.

Grants are supplied only by trusted server code, never derived from a task
prompt, worker-supplied HTTP identifier or untrusted tool arguments. This
module does not execute arbitrary command strings or expose an HTTP action
endpoint. A sensitive callback runs only after an allowed decision commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import time
from types import MappingProxyType
from typing import Callable, TypeVar

from .task_api import OwnerScope, TaskInbox, _TASK_ID
from .task_assignment import validate_worker
from .task_state import TaskState


_T = TypeVar("_T")


class ProtectedAction(str, Enum):
    """Finite, task-related sensitive action categories; no wildcards."""

    READ_SECRET = "read_secret"
    WRITE_WORKSPACE = "write_workspace"
    EXECUTE_TOOL = "execute_tool"
    EXTERNAL_REQUEST = "external_request"
    DELETE_ARTIFACT = "delete_artifact"


class PermissionDenied(PermissionError):
    """Sanitized denial: no principal, paths, prompts or policy details."""


class PermissionUnavailable(RuntimeError):
    """Durable authorization audit is required; never degrade to RAM."""


@dataclass(frozen=True)
class ActionGrant:
    task_id: str
    worker_id: str
    action: ProtectedAction
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not _TASK_ID.fullmatch(self.task_id):
            raise ValueError("grant requires a canonical task ID")
        validate_worker(self.worker_id)
        if not isinstance(self.action, ProtectedAction):
            raise TypeError("grant requires a known protected action")
        if self.expires_at is not None and (
                type(self.expires_at) not in (int, float)
                or not 0 < self.expires_at < 1e12):
            raise ValueError("invalid grant expiration")


@dataclass(frozen=True)
class PermissionAuditEvent:
    event_id: int
    task_id: str
    action: ProtectedAction
    allowed: bool
    reason_code: str
    principal_kind: str
    occurred_at: str


class TaskPermissionGate:
    """Default-deny per-task worker authorization before sensitive actions.

    A configured grant applies to exactly one task, its assigned worker and
    one action. Only trusted Python code may construct the gate and supply
    callbacks. No user/task-generated string can become a grant.
    """

    def __init__(self, inbox: TaskInbox,
                 *, grants: tuple[ActionGrant, ...] = ()) -> None:
        if not isinstance(inbox, TaskInbox) or inbox._store is None:
            raise PermissionUnavailable("private durable task store required")
        if type(grants) is not tuple or any(not isinstance(g, ActionGrant)
                                           for g in grants):
            raise TypeError("grants must be a trusted tuple of ActionGrant")
        rules: dict[tuple[str, str, ProtectedAction], float | None] = {}
        for grant in grants:
            identity = (grant.task_id, grant.worker_id, grant.action)
            if identity in rules:
                raise ValueError("duplicate grant is ambiguous")
            rules[identity] = grant.expires_at
        self.inbox = inbox
        self._grants = MappingProxyType(rules)  # immutable trusted policy

    def run(self, task_id: str, scope: OwnerScope, *, actor_id: str,
            action: ProtectedAction, operation: Callable[[], _T]) -> _T:
        """Persist allow/deny before invoking any side-effecting operation.

        The callback is invoked outside the global inbox lock; its successful
        completion is NOT implied by an allowed authorization audit entry.
        """
        if not isinstance(action, ProtectedAction):
            raise TypeError("unknown protected action")
        if not callable(operation):
            raise TypeError("operation must be a trusted callback")
        validate_worker(actor_id)
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected trusted OwnerScope")
        # Gate-checked callbacks can run after the inbox lock is released.
        # A transfer must wait for them to finish, including their side effects.
        with self.inbox._transfer_barrier(task_id,scope):
            return self._run_under_transfer_barrier(
                task_id,scope,actor_id=actor_id,action=action,operation=operation)

    def _run_under_transfer_barrier(self, task_id: str, scope: OwnerScope,
                                    *, actor_id: str, action: ProtectedAction,
                                    operation: Callable[[], _T]) -> _T:
        with self.inbox._lock:
            record = self.inbox._scoped_record(task_id, scope)
            assignment = self.inbox._assignments.get(task_id)
            matched = assignment is not None and assignment.worker_id == actor_id
            kind = "assigned_worker" if matched else "other_worker"
            if not matched:
                reason = "worker_mismatch"
            elif self.inbox._store.transfer_has_started(task_id):
                # The v14 attempt chain supersedes the legacy immutable
                # assignment; its old grants cannot authorize new actions.
                reason = "transfer_reassigned"
            elif self.inbox._states[task_id] is not TaskState.RUNNING:
                reason = "task_not_running"
            elif self.inbox._store.emergency_held(scope.owner_id, record.project_id):
                reason = "emergency_hold"
            elif ((crisis := self.inbox._store.crisis_child_state(task_id))
                  is not None and
                  (crisis[0] != "assigned" or time.time() >= crisis[1])):
                # An incident that reached HOLD/expired cannot retain its
                # grants even when a worker process has not exited yet.
                reason = "crisis_fenced"
            else:
                identity = (task_id, actor_id, action)
                pin = self.inbox._store.specialist_pin(task_id)
                if pin is not None:
                    try:
                        permitted = json.loads(pin[16])
                        if (type(permitted) is not list
                                or any(type(item) is not str for item in permitted)):
                            raise ValueError("invalid specialist policy")
                    except (ValueError,TypeError) as exc:
                        raise PermissionUnavailable("invalid specialist action policy") from exc
                else:
                    permitted = None
                if pin is not None and (pin[12] != actor_id
                                        or action.value not in permitted):
                    reason = "role_policy_denied"
                elif identity not in self._grants:
                    reason = "grant_missing"
                elif self.inbox._store.is_permission_revoked(task_id, actor_id,
                                                               action.value):
                    reason = "grant_revoked"
                else:
                    deadline = self._grants[identity]
                    reason = ("grant_expired" if deadline is not None
                              and time.time() >= deadline else "granted")
            allowed = reason == "granted"
            # Always commit the sanitized decision before any side effect.
            # Storage failure propagates; no fallback to unaudited execution.
            self.inbox._store.write_permission_audit(
                task_id, action.value, allowed, reason, kind)
        if not allowed:
            raise PermissionDenied("permission_denied")
        return operation()

    def revoke(self, scope: OwnerScope, grant: ActionGrant) -> None:
        """Trusted owner/operator revocation; permanent for this task grant.

        A new task is required for regranting a revoked identity. No model
        text or HTTP request is allowed to invoke this privileged method.
        """
        if not isinstance(scope, OwnerScope) or not isinstance(grant, ActionGrant):
            raise TypeError("trusted scope and grant required")
        with self.inbox._lock:
            self.inbox._scoped_record(grant.task_id, scope)
            self.inbox._store.revoke_permission(grant.task_id, grant.worker_id,
                                                grant.action.value)

    def set_emergency_hold(self, scope: OwnerScope, project_id: str,
                           *, active: bool) -> None:
        """Trusted owner-controlled project stop; applies to old task pins."""
        if (not isinstance(scope, OwnerScope) or type(project_id) is not str
                or project_id not in scope.project_ids or type(active) is not bool):
            raise PermissionDenied("permission_denied")
        with self.inbox._lock:
            self.inbox._store.set_emergency_hold(scope.owner_id, project_id, active)

    def audit_history(self, task_id: str,
                      scope: OwnerScope) -> tuple[PermissionAuditEvent, ...]:
        """Only owner-scoped, pseudonymous metadata; never exposed by HTTP."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected trusted OwnerScope")
        with self.inbox._lock:
            self.inbox._scoped_record(task_id, scope)
            return tuple(PermissionAuditEvent(
                event_id, saved_task, ProtectedAction(action), bool(allowed),
                reason, kind, at)
                for (event_id, saved_task, action, allowed, reason, kind, at)
                in self.inbox._store.permission_history(task_id))
