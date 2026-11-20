"""Stage 5.12: bounded, durable, idempotent trusted task deliveries.

This module does not execute jobs or expose an HTTP dispatch API. Only the
first durable reservation contains a runnable token. A repeated delivery,
including after restart, must not trigger a second execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from secrets import token_hex
import time

from .task_api import OwnerScope, TaskInbox, TaskNotFound
from .task_assignment import validate_worker
from .task_state import TERMINAL_STATES, TaskState
from .task_storage import StorageUnavailable


_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


class DeliveryRejected(RuntimeError):
    """Sanitized invalid delivery/worker/policy operation."""


@dataclass(frozen=True)
class DeliverySnapshot:
    task_id: str
    attempt: int
    max_attempts: int
    status: str
    retry_at: float
    result: str | None


@dataclass(frozen=True)
class DeliveryReservation:
    task_id: str
    attempt: int
    max_attempts: int
    status: str
    should_execute: bool
    token: str | None = field(repr=False)


@dataclass(frozen=True)
class TaskRecoveryEntry:
    """A nonterminal task that must be accounted for after a restart.

    No runnable token or raw task content is exposed. `review_required`
    deliberately forbids an automatic retry of potentially live work.
    """

    task_id: str
    state: TaskState
    delivery_status: str | None
    review_required: bool


class TaskDeliveryManager:
    """Serializes durable deliveries with TaskInbox's existing state lock.

    A trusted adapter must stop/verify its previous process before reporting
    an attempt failed. Unknown in-flight work is **never** automatically
    replayed after restart. Results from outside systems require their own
    stable task_id idempotency key to prevent external side-effect repeats.
    """

    def __init__(self, inbox: TaskInbox, *, retry_delay_seconds: float = 0.05) -> None:
        if not isinstance(inbox, TaskInbox) or inbox._store is None:
            raise DeliveryRejected("durable task inbox required for delivery resume")
        if (type(retry_delay_seconds) not in (int, float)
                or not 0 <= retry_delay_seconds <= 3600):
            raise ValueError("invalid retry delay")
        self.inbox = inbox
        self.retry_delay_seconds = float(retry_delay_seconds)

    @staticmethod
    def _snapshot(task_id: str, row: tuple) -> DeliverySnapshot:
        limit, used, _token, status, retry_at, result = row
        return DeliverySnapshot(task_id, used, limit, status, retry_at, result)

    def _owned(self, task_id: str, scope: OwnerScope, actor_id: str | None) -> None:
        # Called only while the inbox mutex is held.
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected trusted OwnerScope")
        self.inbox._scoped_record(task_id, scope)
        if actor_id is not None:
            assignment = self.inbox._assignments.get(task_id)
            if assignment is None or assignment.worker_id != actor_id:
                raise DeliveryRejected("worker does not own task assignment")

    def reserve(self, task_id: str, scope: OwnerScope, *, actor_id: str,
                max_attempts: int = 3) -> DeliveryReservation:
        """At most one runnable token, 1..5 total attempts, no auto replay."""
        validate_worker(actor_id)
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be 1..5")
        with self.inbox._lock:
            self._owned(task_id, scope, actor_id)
            crisis = self.inbox._store.crisis_child_state(task_id)
            if crisis is not None:
                if max_attempts != 1:
                    # The ordinary worker's retry budget never applies.
                    raise DeliveryRejected("crisis_one_attempt_only")
                if crisis[0] != "assigned" or time.time() >= crisis[1]:
                    raise DeliveryRejected("crisis_not_runnable")
            stored = self.inbox._store.delivery(task_id)
            if stored is not None and stored[0] != max_attempts:
                raise DeliveryRejected("delivery retry policy is immutable")
            state = self.inbox._states[task_id]
            if state in TERMINAL_STATES and (stored is None or stored[3] not in
                                             ("completed", "exhausted")):
                raise DeliveryRejected("terminal task cannot be redelivered")
            if stored is None or stored[3] == "retryable":
                if state not in (TaskState.ASSIGNED, TaskState.RUNNING):
                    raise DeliveryRejected("task is not ready for delivery")
            row, issued = self.inbox._store.begin_delivery(
                task_id, max_attempts, token_hex(16), time.time())
            limit, used, token, status, _, _ = row
            return DeliveryReservation(task_id, used, limit, status,
                                       issued, token if issued else None)

    def report(self, task_id: str, scope: OwnerScope, *, actor_id: str,
               token: str, success: bool) -> DeliverySnapshot:
        """Trusted ACK after verifying worker outcome; duplicate ACK is inert.

        On failure an adapter must verify its process has exited before
        calling this. The manager itself neither runs nor signals processes.
        """
        validate_worker(actor_id)
        if type(success) is not bool:
            raise TypeError("success must be boolean")
        if type(token) is not str or not _TOKEN.fullmatch(token):
            raise DeliveryRejected("invalid attempt token")
        with self.inbox._lock:
            self._owned(task_id, scope, actor_id)
            stored = self.inbox._store.delivery(task_id)
            if stored is None or stored[2] != token:
                raise DeliveryRejected("unknown or superseded attempt token")
            limit, used, _, status, retry_at, result = stored
            expected = "success" if success else "failure"
            if status != "active":
                if result != expected:
                    raise DeliveryRejected("conflicting attempt result")
                return self._snapshot(task_id, stored)
            state = self.inbox._states[task_id]
            if success:
                # The 5.11 controller must have verified process completion
                # and durably marked SUCCEEDED *before* this delivery ACK.
                if state is not TaskState.SUCCEEDED:
                    raise DeliveryRejected("successful execution is not finalized")
            elif state not in (TaskState.ASSIGNED, TaskState.RUNNING):
                raise DeliveryRejected("task cannot accept failed attempt")
            backoff = self.retry_delay_seconds * (2 ** (used - 1))
            row, written = self.inbox._store.settle_delivery(
                task_id, token, success, time.time() + backoff)
            if not written:
                raise StorageUnavailable("delivery acknowledgement not committed")
            return self._snapshot(task_id, row)

    def snapshot(self, task_id: str, scope: OwnerScope) -> DeliverySnapshot | None:
        """Resume metadata; never expose/reissue an active runnable token."""
        with self.inbox._lock:
            self._owned(task_id, scope, None)
            row = self.inbox._store.delivery(task_id)
            return None if row is None else self._snapshot(task_id, row)

    def resume_pending(self, scope: OwnerScope) -> tuple[DeliverySnapshot, ...]:
        """After SQLite restart inspect active/retryable attempts, fail closed."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected trusted OwnerScope")
        with self.inbox._lock:
            found = []
            for task_id, record in self.inbox._by_id.items():
                if (record.owner_id != scope.owner_id
                        or record.project_id not in scope.project_ids):
                    continue
                if self.inbox._states[task_id] in TERMINAL_STATES:
                    continue
                row = self.inbox._store.delivery(task_id)
                if row is not None and row[3] in ("active", "retryable"):
                    found.append(self._snapshot(task_id, row))
            return tuple(found)

    def recovery_inventory(self, scope: OwnerScope) -> tuple[TaskRecoveryEntry, ...]:
        """Enumerate *every* nonterminal task, including missing ledgers.

        `resume_pending` intentionally lists only deliveries; a RUNNING task
        can exist without a delivery row when attached by trusted code. This
        offline inventory closes that blind spot, but never assumes an
        orphaned process was stopped or reissues an unknown active ticket.
        After an unclean restart, a trusted operator must independently
        verify worker/process outcomes before transitioning such tasks.
        """
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected trusted OwnerScope")
        with self.inbox._lock:
            entries = []
            for task_id, record in self.inbox._by_id.items():
                if (record.owner_id != scope.owner_id
                        or record.project_id not in scope.project_ids):
                    continue
                state = self.inbox._states[task_id]
                if state in TERMINAL_STATES:
                    continue
                row = self.inbox._store.delivery(task_id)
                delivery_status = None if row is None else row[3]
                # A claimed workspace may contain work even without a
                # delivery ledger. Only an unclaimed QUEUED task is safe
                # to consider for normal dispatch after a clean restart.
                review_required = (state is not TaskState.QUEUED
                                   or delivery_status is not None)
                entries.append(TaskRecoveryEntry(task_id, state,
                                                 delivery_status,
                                                 review_required))
            return tuple(entries)
