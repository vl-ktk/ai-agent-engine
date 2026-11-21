"""7.11: shared, durable admission gate for already approved GPT/Claude work.

This trusted offline gate does not launch models or allocate an OS CPU core.
Logical agents share one controller pool; no per-avatar subprocess exists.
The existing SQLite inbox remains the task queue. Stage 7.12 will own retry
scheduling and provider-unavailability detection.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable, TypeVar
from uuid import uuid4

from .main_specialists import load_specialist_pin
from .task_api import OwnerScope, TaskChoice, TaskInbox, TaskNotFound
from .task_assignment import validate_worker
from .task_state import TaskState, TERMINAL_STATES


_R = TypeVar("_R")


class CapacityDenied(PermissionError):
    """Generic refusal without owner, project, secret or prompt details."""


@dataclass(frozen=True, slots=True)
class ProviderBudget:
    provider: str
    parallel_slots: int
    calls_per_window: int
    window_seconds: int

    def __post_init__(self) -> None:
        if (type(self.provider) is not str or self.provider not in ("gpt", "claude")
                or type(self.parallel_slots) is not int
                or not 1 <= self.parallel_slots <= 64
                or type(self.calls_per_window) is not int
                or not 1 <= self.calls_per_window <= 100_000
                or type(self.window_seconds) is not int
                or not 60 <= self.window_seconds <= 86_400):
            raise ValueError("invalid trusted provider budget")


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    cpu_slots: int
    providers: tuple[ProviderBudget, ...]

    def __post_init__(self) -> None:
        if (type(self.cpu_slots) is not int or not 1 <= self.cpu_slots <= 64
                or type(self.providers) is not tuple or not self.providers
                or any(type(p) is not ProviderBudget for p in self.providers)
                or len(set(p.provider for p in self.providers)) != len(self.providers)):
            raise ValueError("invalid trusted shared capacity policy")


@dataclass(frozen=True, slots=True)
class CapacityDecision:
    admitted: bool
    reason: str
    reservation_id: str | None = None
    retry_after_seconds: float | None = None


class SharedCapacity:
    """Single, cross-project controller admission point using private SQLite.

    Call the trusted model adapter *only after* admission. The reservation
    remains active after crashes/unknown process state and is released only
    following observed terminal/no-launch evidence. This prevents a second
    logical agent from silently overbooking the same provider or CPU slot.
    """

    def __init__(self, inbox: TaskInbox, policy: CapacityPolicy,
                 *, clock: Callable[[], float] = time.time) -> None:
        if (not isinstance(inbox, TaskInbox) or not inbox.durable
                or not isinstance(policy, CapacityPolicy) or not callable(clock)):
            raise CapacityDenied("durable_trusted_capacity_required")
        self.inbox = inbox
        self.policy = policy
        self.clock = clock

    def _now(self) -> float:
        now = self.clock()
        if type(now) not in (float, int) or not math.isfinite(now) or now < 0:
            raise CapacityDenied("invalid_capacity_clock")
        return float(now)

    def waiting(self, scope: OwnerScope) -> tuple[TaskChoice, ...]:
        """Snapshot of approved, assigned tasks; never starts a worker.

        Priority desc and admission receipt asc are inherited from stage 5.
        A slot-denied task stays ASSIGNED in this durable queue, not FAILED.
        """
        if not isinstance(scope, OwnerScope):
            raise CapacityDenied("trusted_owner_scope_required")
        with self.inbox._lock:
            choices = []
            for record in self.inbox._pending:
                if (record.owner_id != scope.owner_id
                        or record.project_id not in scope.project_ids
                        or self.inbox._states[record.task_id] is not TaskState.ASSIGNED
                        or self.inbox._store.capacity_reservation(record.task_id)
                        or self.inbox._store.emergency_held(scope.owner_id,
                                                            record.project_id)
                        or self.inbox._store.transfer_has_started(record.task_id)):
                    continue
                pin = load_specialist_pin(self.inbox, scope, record.task_id)
                if pin is not None:
                    choices.append(TaskChoice(record.task_id, record.project_id,
                        self.inbox._priorities[record.task_id], record.receipt))
            return tuple(sorted(choices, key=lambda c:
                                (-c.priority, c.receipt, c.task_id)))

    def acquire(self, task_id: str, scope: OwnerScope,
                *, worker_id: str) -> CapacityDecision:
        if not isinstance(scope, OwnerScope):
            raise CapacityDenied("trusted_owner_scope_required")
        validate_worker(worker_id)
        with self.inbox._lock:
            try:
                record = self.inbox._scoped_record(task_id, scope)
            except TaskNotFound:
                raise CapacityDenied("task_outside_owner_scope") from None
            assignment = self.inbox._assignments.get(task_id)
            if (assignment is None or assignment.worker_id != worker_id
                    or self.inbox._states[task_id] is not TaskState.ASSIGNED
                    or self.inbox._store.transfer_has_started(task_id)
                    or self.inbox._store.emergency_held(scope.owner_id,
                                                        record.project_id)):
                raise CapacityDenied("task_not_ready_for_capacity")
            pin = load_specialist_pin(self.inbox, scope, task_id)
            if pin is None or pin.agent_id != worker_id:
                raise CapacityDenied("approved_specialist_required")
            # WEB-12: the reviewed chatgpt_web channel consumes the *same*
            # existing GPT capacity ledger. No new provider class, schema
            # migration, allowance or bypass of owner-pinned specialists.
            capacity_provider = ("gpt" if pin.provider_id == "chatgpt_web"
                                 else pin.provider_id)
            budget = next((p for p in self.policy.providers
                           if p.provider == capacity_provider), None)
            if budget is None:
                return CapacityDecision(False, "provider_not_enabled")
            now = self._now()
            reservation_id = "capacity_" + uuid4().hex
            reason, retry_after = self.inbox._store.reserve_capacity(
                reservation_id=reservation_id, task_id=task_id,
                owner_id=scope.owner_id, project_id=record.project_id,
                worker_id=worker_id, provider=budget.provider,
                cpu_slots=self.policy.cpu_slots,
                provider_slots=budget.parallel_slots,
                max_calls=budget.calls_per_window,
                window_seconds=budget.window_seconds, now=now)
            return CapacityDecision(reason == "admitted", reason,
                reservation_id if reason == "admitted" else None, retry_after)

    def release(self, task_id: str, scope: OwnerScope,
                *, worker_id: str, reservation_id: str) -> bool:
        """Only exact worker and terminal, verified-stopped work can release.

        An uncertain RUNNING process keeps the slot across process restarts.
        Quota call history is never refunded or removed on release.
        """
        if (not isinstance(scope, OwnerScope)
                or type(reservation_id) is not str
                or not reservation_id.startswith("capacity_")):
            raise CapacityDenied("invalid_capacity_receipt")
        validate_worker(worker_id)
        with self.inbox._lock:
            try:
                self.inbox._scoped_record(task_id, scope)
            except TaskNotFound:
                raise CapacityDenied("task_outside_owner_scope") from None
            assignment = self.inbox._assignments.get(task_id)
            if assignment is None or assignment.worker_id != worker_id:
                raise CapacityDenied("worker_mismatch")
            state = self.inbox._states[task_id]
            process = self.inbox._store.process_record(task_id)
            if state not in TERMINAL_STATES:
                raise CapacityDenied("unverified_execution_state")
            if process is not None and process[-1] != "stopped":
                raise CapacityDenied("unverified_process_stop")
            return self.inbox._store.release_capacity(
                reservation_id, task_id, worker_id, self._now())

    def run(self, task_id: str, scope: OwnerScope, *, worker_id: str,
            operation: Callable[[], _R]) -> tuple[CapacityDecision, _R | None]:
        """Trusted one-shot adapter wrapper; never retry on a denied slot.

        A failure that leaves RUNNING/unknown state conservatively keeps its
        reservation for explicit recovery. This wrapper does not grant access.
        """
        if not callable(operation):
            raise CapacityDenied("trusted_operation_required")
        decision = self.acquire(task_id, scope, worker_id=worker_id)
        if not decision.admitted:
            return decision, None
        try:
            return decision, operation()
        finally:
            try:
                self.release(task_id, scope, worker_id=worker_id,
                             reservation_id=decision.reservation_id)
            except CapacityDenied:
                # Never free an unverified running process after a failure.
                pass
