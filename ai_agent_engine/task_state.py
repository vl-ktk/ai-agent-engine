"""Stage 5.7: authoritative task lifecycle vocabulary and transition graph.

Only QUEUED and ASSIGNED are observable in the 5.7 inbox. Other states are
reserved for trusted execution/cancellation steps; defining an edge does not
authorize an HTTP client to perform it.
"""

from __future__ import annotations

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
