"""Stages 5.7-5.8: task lifecycle vocabulary and audited transitions.

Transition requests remain internal and trusted, never exposed as HTTP writes.
The graph does not authorize an HTTP client or launch any real worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class TaskState(str, Enum):
    """Stable lowercase values for storage and future execution adapters."""

    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TransitionRejected(RuntimeError):
    """A well-formed transition was rejected; its safe reason was audited."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class TaskTransitionEvent:
    """Metadata-only audit event; no prompt, workspace path or credentials."""

    event_id: int
    task_id: str
    from_state: TaskState
    to_state: TaskState
    actor_id: str
    accepted: bool
    reason_code: str
    occurred_at: str


TERMINAL_STATES = frozenset({
    TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
    TaskState.TIMED_OUT,
})

# This schema is intentionally immutable: an additional state or edge is a
# contract change, not a side effect of user-supplied JSON or configuration.
ALLOWED_TRANSITIONS = MappingProxyType({
    TaskState.QUEUED: frozenset({TaskState.ASSIGNED, TaskState.CANCELLED}),
    TaskState.ASSIGNED: frozenset({
        TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED,
        TaskState.TIMED_OUT,
    }),
    TaskState.RUNNING: frozenset({
        TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
        TaskState.TIMED_OUT,
    }),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.TIMED_OUT: frozenset(),
})


def allowed_next_states(state: TaskState) -> frozenset[TaskState]:
    """Read-only schema; accepting plain strings could bypass validation."""
    if not isinstance(state, TaskState):
        raise TypeError("expected TaskState")
    return ALLOWED_TRANSITIONS[state]


def can_transition(current: TaskState, target: TaskState) -> bool:
    """Pure policy decision; persistence/authorization belong to later stages."""
    if not isinstance(target, TaskState):
        raise TypeError("expected TaskState")
    return target in allowed_next_states(current)
