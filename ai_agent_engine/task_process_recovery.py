"""Stage 6.9: supervised orphan cleanup after unclean controller shutdown.

Only the already-assigned owner-scoped SQLite records authorize a recovery.
No raw PID from user/model input can select a process or group to signal.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import time

from .task_api import OwnerScope, TaskInbox
from .task_process_identity import (
    ProcessIdentity, ProcessIdentityUnavailable, group_has_live_members,
    matches_leader, verified_leader_pidfd,
)
from .task_state import TERMINAL_STATES, TaskState


class RecoveryDenied(RuntimeError):
    """Sanitized refusal to risk terminating the wrong process."""


@dataclass(frozen=True, slots=True)
class ProcessRecoveryResult:
    task_id: str
    status: str  # stopped | held_identity | held_untracked | held_unverified


class TaskProcessRecovery:
    """One trusted restart pass; never retries a previously running task."""

    def __init__(self, inbox: TaskInbox, scope: OwnerScope, *,
                 grace_seconds: float = 0.15) -> None:
        if (not isinstance(inbox, TaskInbox) or inbox._store is None
                or not isinstance(scope, OwnerScope)
                or type(grace_seconds) not in (int, float)
                or not 0.01 <= grace_seconds <= 3):
            raise RecoveryDenied("trusted durable recovery context required")
        if not hasattr(os, "pidfd_open"):
            raise RecoveryDenied("safe process identity verification unavailable")
        self.inbox = inbox
        self.scope = scope
        self.grace_seconds = float(grace_seconds)

    @staticmethod
    def _wait_group(pgid: int, seconds: float) -> bool:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if not group_has_live_members(pgid):
                return True
            time.sleep(0.01)
        return not group_has_live_members(pgid)

    def _stop_verified(self, identity: ProcessIdentity) -> bool:
        """Hold a pidfd over group signals, then check all live members gone."""
        fd = verified_leader_pidfd(identity)
        try:
            if identity.pgid == os.getpgrp() or not matches_leader(identity):
                raise ProcessIdentityUnavailable("unsafe process group identity")
            try:
                os.killpg(identity.pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if self._wait_group(identity.pgid, self.grace_seconds):
                return True
            # pidfd pins the original leader ID against reuse. If the group
            # remains live, it cannot have been repurposed for another task.
            try:
                os.killpg(identity.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return self._wait_group(identity.pgid, 2.0)
        finally:
            os.close(fd)

    def _recover_one(self, task_id: str, state: TaskState, row: tuple) -> str:
        worker_id, pid, pgid, sid, start, boot, kind, status = row
        assigned = self.inbox.get_assignment(task_id, self.scope)
        if assigned is None or assigned.worker_id != worker_id:
            return "held_identity"
        identity = ProcessIdentity(pid, pgid, sid, start, boot)
        try:
            if status == "stopped":
                if group_has_live_members(pgid):
                    return "held_unverified"
            elif matches_leader(identity):
                if not self._stop_verified(identity):
                    return "held_unverified"
            else:
                # Never kill a foreign process with a reused PID. If the
                # original leader vanished and its group is empty, it is
                # safe to retire the logical task but NOT to replay its work.
                from .task_process_identity import _boot_id, _proc_details
                if (_boot_id() == boot and _proc_details(pid) is not None):
                    return "held_identity"
                if group_has_live_members(pgid):
                    return "held_unverified"
        except (ProcessIdentityUnavailable, PermissionError, OSError):
            return "held_unverified"
        if group_has_live_members(pgid):
            return "held_unverified"
        # No live group remains. Preserve the fact that an external request
        # may have occurred: cancellation is not a provider success/ACK.
        self.inbox.transition_state(task_id, TaskState.CANCELLED, self.scope,
                                    actor_id="orchestrator")
        if status == "active":
            with self.inbox._lock:
                self.inbox._store.mark_process_stopped(task_id, pid, start, boot)
        return "stopped"

    def recover(self) -> tuple[ProcessRecoveryResult, ...]:
        """Review each owned nonterminal assignment with no implicit retry."""
        results = []
        # Exclusive SQLite and workspace flock prevent concurrent controllers
        # in this host; do not hold task mutex across group signals.
        with self.inbox._lock:
            inventory = tuple((task_id, state)
                              for task_id, record in self.inbox._by_id.items()
                              if record.owner_id == self.scope.owner_id
                              and record.project_id in self.scope.project_ids
                              for state in (self.inbox._states[task_id],)
                              if state not in TERMINAL_STATES
                              and state is not TaskState.QUEUED)
        for task_id, state in inventory:
            with self.inbox._lock:
                row = self.inbox._store.process_record(task_id)
            if row is None:
                if state is TaskState.RUNNING:
                    results.append(ProcessRecoveryResult(task_id, "held_untracked"))
                continue
            results.append(ProcessRecoveryResult(task_id,
                          self._recover_one(task_id, state, row)))
        return tuple(results)
