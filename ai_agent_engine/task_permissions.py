"""Stage 5.13: fail-closed action grants and metadata-only durable audit.

Grants are supplied only by trusted server code, never derived from a task
prompt, worker-supplied HTTP identifier or untrusted tool arguments. This
module does not execute arbitrary command strings or expose an HTTP action
endpoint. A sensitive callback runs only after an allowed decision commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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
        with self.inbox._lock:
            self.inbox._scoped_record(task_id, scope)
            assignment = self.inbox._assignments.get(task_id)
            matched = assignment is not None and assignment.worker_id == actor_id
            kind = "assigned_worker" if matched else "other_worker"
            if not matched:
                reason = "worker_mismatch"
            elif self.inbox._states[task_id] is not TaskState.RUNNING:
                reason = "task_not_running"
            else:
                identity = (task_id, actor_id, action)
                if identity not in self._grants:
                    reason = "grant_missing"
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
