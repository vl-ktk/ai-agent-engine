"""Stage 5.11: trusted local cancellation and timeout for managed processes.

No HTTP process launching, shell execution, model adapter or public API.
The caller must supply an already-created subprocess.Popen whose leader is
its own process group (start_new_session=True on Linux). A task is marked
terminal only after its managed child has been stopped or reaped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import subprocess
import sys
from threading import Event, Lock, Thread, current_thread
import time
from typing import Callable, TypeVar

from .task_api import OwnerScope, TaskInbox, TaskNotFound
from .task_permissions import PermissionUnavailable, ProtectedAction, TaskPermissionGate
from .task_process_identity import (
    ProcessIdentity, ProcessIdentityUnavailable, capture_private_leader,
    matches_leader,
)
from .task_state import TaskState, TransitionRejected


_T = TypeVar("_T")


class TaskControlError(RuntimeError):
    """Sanitized refusal to manage or terminate an unsafe process."""


@dataclass
class _ManagedRun:
    task_id: str
    process: subprocess.Popen
    pgid: int
    scope: OwnerScope
    worker_id: str
    identity: ProcessIdentity | None
    deadline: float | None
    lock: Lock = field(default_factory=Lock)
    done: bool = False


class TaskProcessController:
    """Process-local supervisor of already-started, isolated child groups.

    Only trusted Python calls can attach/cancel/finish; there is no HTTP
    transition endpoint. Each run has its own lock. The global registry lock
    is never held during a signal, process wait or SQLite state transition.
    """

    def __init__(self, inbox: TaskInbox, *, grace_seconds: float = 0.15,
                 check_interval: float = 0.05,
                 permission_gate: TaskPermissionGate | None = None) -> None:
        if not isinstance(inbox, TaskInbox):
            raise TypeError("expected TaskInbox")
        for value in (grace_seconds, check_interval):
            if (type(value) not in (int, float) or not 0.01 <= value <= 5):
                raise ValueError("invalid control interval")
        self.inbox = inbox
        if permission_gate is not None and (
                not isinstance(permission_gate, TaskPermissionGate)
                or permission_gate.inbox is not inbox):
            raise TypeError("permission gate must belong to the task inbox")
        self.permission_gate = permission_gate
        self.grace_seconds = float(grace_seconds)
        self.check_interval = float(check_interval)
        self._mutex = Lock()
        self._runs: dict[str, _ManagedRun] = {}
        self._shutdown = Event()
        self._watchdog: Thread | None = None
        self._closed = False
        self._watchdog_errors: list[str] = []

    @staticmethod
    def _validate_process(process: subprocess.Popen) -> int:
        """Refuse caller's process group: never signal the engine itself."""
        if not isinstance(process, subprocess.Popen):
            raise TaskControlError("expected managed subprocess handle")
        if process.pid == os.getpid() or process.poll() is not None:
            raise TaskControlError("managed process is not running")
        try:
            pgid = os.getpgid(process.pid)
            sid = os.getsid(process.pid)
        except OSError as exc:
            raise TaskControlError("managed process group unavailable") from exc
        if (pgid != process.pid or pgid == os.getpgrp()
                or sid != process.pid):
            raise TaskControlError("process must have its own session")
        return pgid

    def attach(self, task_id: str, process: subprocess.Popen, scope: OwnerScope,
               *, actor_id: str, timeout_seconds: float | None = None,
               _launch_kind: str = "direct") -> None:
        """Attach an existing isolated child and begin a trusted RUNNING state.

        The caller retains ownership of an invalid/unattached process and
        must clean it up; this method never kills a process it did not accept.
        """
        if timeout_seconds is not None and (type(timeout_seconds) not in (int, float)
                                             or not 0.01 <= timeout_seconds <= 86400):
            raise ValueError("invalid timeout")
        pgid = self._validate_process(process)
        if _launch_kind not in ("direct", "gated"):
            raise TaskControlError("invalid trusted process launch kind")
        with self._mutex:
            if self._closed:
                raise TaskControlError("task controller is closed")
            if task_id in self._runs:
                raise TaskControlError("task already has a managed process")
            if any(run.pgid == pgid and not run.done for run in self._runs.values()):
                raise TaskControlError("process group already belongs to a task")
            assignment = self.inbox.get_assignment(task_id, scope)
            if assignment is None:
                raise TaskNotFound("task has no assignment in owner scope")
            if self.inbox._store is not None:
                with self.inbox._lock:
                    if self.inbox._store.transfer_has_started(task_id):
                        raise TaskControlError("task assignment was superseded")
            if actor_id != assignment.worker_id:
                raise TaskControlError("worker does not own assigned task")
            if self.inbox.state(task_id, scope) is not TaskState.ASSIGNED:
                raise TaskControlError("task is not waiting for a worker")
            identity = None
            if self.inbox._store is not None:
                try:
                    identity = capture_private_leader(process.pid)
                except ProcessIdentityUnavailable:
                    raise TaskControlError("cannot establish process identity") from None
                with self.inbox._lock:
                    self.inbox._store.register_process(
                        task_id, actor_id, pid=identity.pid, pgid=identity.pgid,
                        sid=identity.sid, start_ticks=identity.start_ticks,
                        boot_id=identity.boot_id, launch_kind=_launch_kind)
            self.inbox.transition_state(task_id, TaskState.RUNNING, scope,
                                        actor_id=actor_id)
            deadline = (time.monotonic() + timeout_seconds
                        if timeout_seconds is not None else None)
            self._runs[task_id] = _ManagedRun(task_id, process, pgid, scope,
                                               actor_id, identity, deadline)
            if deadline is not None and self._watchdog is None:
                self._watchdog = Thread(target=self._watch, daemon=True,
                                        name="ae-task-timeouts")
                self._watchdog.start()

    def launch_gated(self, task_id: str, scope: OwnerScope, *, actor_id: str,
                     worker_argv: tuple[str, ...], cwd: str,
                     environment: dict[str, str],
                     timeout_seconds: float | None = None) -> subprocess.Popen:
        """Start a dormant child; commit its identity before releasing exec.

        A pre-attach SIGKILL of this supervisor closes the one-byte pipe.
        The child then exits without ever executing the provider worker.
        """
        if self.inbox._store is None:
            raise TaskControlError("gated launch requires durable task database")
        if (type(worker_argv) is not tuple or not worker_argv
                or any(type(item) is not str or not item or "\x00" in item
                       for item in worker_argv)
                or type(cwd) is not str or not cwd.startswith("/")
                or not isinstance(environment, dict)):
            raise TaskControlError("invalid trusted worker launch")
        assignment = self.inbox.get_assignment(task_id, scope)
        if (assignment is None or assignment.worker_id != actor_id
                or assignment.workspace_dir != cwd):
            raise TaskControlError("trusted workspace assignment required")
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        process = None
        attached = False
        try:
            argv = (sys.executable, "-m", "ai_agent_engine.task_launch_gate",
                    str(read_fd), *worker_argv)
            process = subprocess.Popen(
                argv, cwd=cwd, env=environment,
                pass_fds=(read_fd,), close_fds=True,
                start_new_session=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            os.close(read_fd)
            read_fd = -1
            self.attach(task_id, process, scope, actor_id=actor_id,
                        timeout_seconds=timeout_seconds,
                        _launch_kind="gated")
            attached = True
            if os.write(write_fd, b"G") != 1:
                raise TaskControlError("gated worker did not start")
            return process
        except BaseException:
            if attached:
                try:
                    self.cancel(task_id, scope)
                except Exception:
                    pass  # original attach/launch exception remains primary
            if process is not None:
                try:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            raise
        finally:
            if read_fd != -1:
                os.close(read_fd)
            os.close(write_fd)

    def _mark_stopped(self, run: _ManagedRun) -> None:
        if self.inbox._store is not None and run.identity is not None:
            with self.inbox._lock:
                self.inbox._store.mark_process_stopped(
                    run.task_id, run.identity.pid, run.identity.start_ticks,
                    run.identity.boot_id)

    @staticmethod
    def _group_running(pgid: int) -> bool:
        """Distinguish live Linux descendants from exited, unreaped zombies.

        killpg(group, 0) also returns success for zombie-only groups. A
        terminal task must have no executable members, including children of
        the direct child that the controller cannot waitpid() itself.
        """
        try:
            with os.scandir("/proc") as entries:
                for entry in entries:
                    if not entry.name.isdecimal():
                        continue
                    try:
                        with open(f"/proc/{entry.name}/stat", encoding="ascii") as stat:
                            content = stat.read()
                        fields = content.rsplit(")", 1)[1].split()
                        if (int(fields[2]) == pgid
                                and fields[0] not in ("Z", "X", "x")):
                            return True
                    except (FileNotFoundError, ProcessLookupError):
                        continue  # process exited during inspection
            return False
        except (OSError, ValueError, IndexError) as exc:
            raise TaskControlError("cannot verify managed process group stop") from exc

    def _terminate_group(self, run: _ManagedRun) -> None:
        """TERM whole private group, escalate to KILL, reap direct child."""
        if (run.process.poll() is None and run.identity is not None
                and not matches_leader(run.identity)):
            raise TaskControlError("managed process identity changed")
        if run.process.poll() is None:
            try:
                os.killpg(run.pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise TaskControlError("cannot stop managed process") from exc
        deadline = time.monotonic() + self.grace_seconds
        while time.monotonic() < deadline and self._group_running(run.pgid):
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        if self._group_running(run.pgid):
            try:
                os.killpg(run.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise TaskControlError("cannot force-stop managed process") from exc
        try:
            run.process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise TaskControlError("managed process could not be reaped") from exc
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self._group_running(run.pgid):
            time.sleep(0.01)
        if run.process.poll() is None or self._group_running(run.pgid):
            raise TaskControlError("managed process group did not terminate")

    def _stop_run(self, run: _ManagedRun, target: TaskState) -> TaskState:
        with run.lock:
            if run.done:
                raise TransitionRejected("invalid_edge")
            self._terminate_group(run)
            # The existing workspace lease is released only AFTER durable
            # state/audit commit. On DB failure it stays reserved for retry.
            self.inbox.transition_state(run.task_id, target, run.scope,
                                        actor_id="orchestrator")
            run.done = True
            self._mark_stopped(run)
            return target

    def cancel(self, task_id: str, scope: OwnerScope) -> TaskState:
        """Cancel a scoped task, stopping a registered process before status."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected trusted OwnerScope")
        with self._mutex:
            if self._closed:
                raise TaskControlError("task controller is closed")
            state = self.inbox.state(task_id, scope)
            if state is None:
                raise TaskNotFound("task outside owner scope")
            run = self._runs.get(task_id)
            if run is None:
                if state is TaskState.RUNNING:
                    raise TaskControlError("running process is not managed")
                return self.inbox.transition_state(task_id, TaskState.CANCELLED,
                                                   scope, actor_id="orchestrator")
            if run.done and state is TaskState.RUNNING:
                # A failed attempt was stopped but the next attempt is not
                # attached yet. Hold the registry lock against attach_retry.
                return self.inbox.transition_state(task_id, TaskState.CANCELLED,
                                                   scope, actor_id="orchestrator")
        return self._stop_run(run, TaskState.CANCELLED)

    def finish(self, task_id: str, scope: OwnerScope, *, success: bool) -> TaskState:
        """A trusted adapter may finalize only an already-exited child."""
        if type(success) is not bool:
            raise TypeError("expected boolean success")
        with self._mutex:
            if self._closed:
                raise TaskControlError("task controller is closed")
            if self.inbox.state(task_id, scope) is None:
                raise TaskNotFound("task outside owner scope")
            run = self._runs.get(task_id)
            if run is None:
                raise TaskControlError("running process is not managed")
        with run.lock:
            if run.done:
                raise TransitionRejected("invalid_edge")
            if run.deadline is not None and time.monotonic() >= run.deadline:
                raise TaskControlError("managed process deadline expired")
            return_code = run.process.poll()
            if return_code is None or self._group_running(run.pgid):
                raise TaskControlError("managed process group is still running")
            if success != (return_code == 0):
                raise TaskControlError("result does not match process exit status")
            run.process.wait(timeout=0)
            target = TaskState.SUCCEEDED if success else TaskState.FAILED
            self.inbox.transition_state(task_id, target, scope,
                                        actor_id=run.worker_id)
            run.done = True
            self._mark_stopped(run)
            return target

    def complete_failed_attempt(self, task_id: str, scope: OwnerScope) -> int:
        """5.12: verify failed attempt ended without finalizing logical task.

        The trusted adapter may then ACK failure to TaskDeliveryManager and
        reserve another bounded attempt. A failure cannot release the lease
        or fabricate an allowed transition from a terminal task.
        """
        with self._mutex:
            if self._closed or self.inbox.state(task_id, scope) is not TaskState.RUNNING:
                raise TaskControlError("task is not running")
            run = self._runs.get(task_id)
            if run is None:
                raise TaskControlError("task has no managed process")
        with run.lock:
            if run.done:
                raise TransitionRejected("invalid_edge")
            code = run.process.poll()
            if code is None or self._group_running(run.pgid):
                raise TaskControlError("attempt process group is still running")
            if code == 0:
                raise TaskControlError("successful process is not a failed attempt")
            run.process.wait(timeout=0)
            run.done = True
            self._mark_stopped(run)
            return code

    def attach_retry(self, task_id: str, process: subprocess.Popen,
                     scope: OwnerScope, *, actor_id: str, delivery_token: str,
                     timeout_seconds: float | None = None) -> None:
        """Attach a new child only for an issued durable retry ticket.

        All earlier group members must have stopped and been acknowledged.
        State stays RUNNING; the original workspace lease remains exclusive.
        """
        if timeout_seconds is not None and (type(timeout_seconds) not in (int, float)
                                             or not 0.01 <= timeout_seconds <= 86400):
            raise ValueError("invalid timeout")
        pgid = self._validate_process(process)
        with self._mutex:
            if self._closed:
                raise TaskControlError("task controller is closed")
            previous = self._runs.get(task_id)
            if previous is None or not previous.done:
                raise TaskControlError("previous attempt has not stopped")
            if any(run.pgid == pgid and not run.done for run in self._runs.values()):
                raise TaskControlError("process group already belongs to a task")
            assignment = self.inbox.get_assignment(task_id, scope)
            if assignment is None or assignment.worker_id != actor_id:
                raise TaskControlError("worker does not own task assignment")
            with self.inbox._lock:
                if self.inbox._store.transfer_has_started(task_id):
                    raise TaskControlError("task assignment was superseded")
            if self.inbox.state(task_id, scope) is not TaskState.RUNNING:
                raise TaskControlError("retry requires a running logical task")
            if self.inbox._store is None:
                raise TaskControlError("durable retry ledger is required")
            with self.inbox._lock:
                row = self.inbox._store.delivery(task_id)
            if (row is None or row[1] < 2 or row[2] != delivery_token
                    or row[3] != "active"):
                raise TaskControlError("retry ticket is not active")
            identity = None
            if self.inbox._store is not None:
                try:
                    identity = capture_private_leader(process.pid)
                except ProcessIdentityUnavailable:
                    raise TaskControlError("cannot establish retry identity") from None
                with self.inbox._lock:
                    self.inbox._store.register_process(
                        task_id, actor_id, pid=identity.pid, pgid=identity.pgid,
                        sid=identity.sid, start_ticks=identity.start_ticks,
                        boot_id=identity.boot_id, launch_kind="direct")
            deadline = (time.monotonic() + timeout_seconds
                        if timeout_seconds is not None else None)
            self._runs[task_id] = _ManagedRun(task_id, process, pgid, scope,
                                               actor_id, identity, deadline)
            if deadline is not None and self._watchdog is None:
                self._watchdog = Thread(target=self._watch, daemon=True,
                                        name="ae-task-timeouts")
                self._watchdog.start()

    def finalize_exhausted(self, task_id: str, scope: OwnerScope) -> TaskState:
        """5.12: close failed logical task only after all safe retries end."""
        with self._mutex:
            if self._closed:
                raise TaskControlError("task controller is closed")
            run = self._runs.get(task_id)
            if (run is None or not run.done
                    or self.inbox.state(task_id, scope) is not TaskState.RUNNING):
                raise TaskControlError("failed process is not safely stopped")
            if self.inbox._store is None:
                raise TaskControlError("durable retry ledger is required")
            with self.inbox._lock:
                row = self.inbox._store.delivery(task_id)
            if row is None or row[3] != "exhausted":
                raise TaskControlError("delivery attempts are not exhausted")
            return self.inbox.transition_state(task_id, TaskState.FAILED,
                                               scope, actor_id=run.worker_id)

    def execute_protected(self, task_id: str, scope: OwnerScope,
                          *, actor_id: str, action: ProtectedAction,
                          operation: Callable[[], _T]) -> _T:
        """5.13: guard a sensitive callback while its managed job stays live.

        Missing gate refuses by default. The per-run lock prevents a normal
        cancel/timeout from completing while the trusted action is in flight.
        No arbitrary command string or HTTP endpoint is introduced.
        """
        if self.permission_gate is None:
            raise PermissionUnavailable("protected execution needs trusted grants")
        with self._mutex:
            if self._closed:
                raise TaskControlError("task controller is closed")
            run = self._runs.get(task_id)
            if run is None:
                raise TaskControlError("task has no managed process")
        with run.lock:
            if (run.done or run.process.poll() is not None
                    or self.inbox.state(task_id, scope) is not TaskState.RUNNING
                    or (run.deadline is not None
                        and time.monotonic() >= run.deadline)):
                raise TaskControlError("managed task is not executing")
            return self.permission_gate.run(
                task_id, scope, actor_id=actor_id,
                action=action, operation=operation)

    def _watch(self) -> None:
        while not self._shutdown.wait(self.check_interval):
            with self._mutex:
                runs = tuple(self._runs.values())
            for run in runs:
                if run.deadline is None or run.done or time.monotonic() < run.deadline:
                    continue
                try:
                    self._stop_run(run, TaskState.TIMED_OUT)
                except TransitionRejected:
                    pass  # completed concurrently
                except Exception:
                    # Sanitized flag, retried on next tick. Never report an
                    # uncommitted timeout as successful.
                    with self._mutex:
                        if len(self._watchdog_errors) < 32:
                            self._watchdog_errors.append("timeout_stop_failed")

    @property
    def watchdog_errors(self) -> tuple[str, ...]:
        with self._mutex:
            return tuple(self._watchdog_errors)

    def close(self) -> None:
        """Stop all managed live children and end the watchdog thread."""
        self._shutdown.set()
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            runs = tuple(self._runs.values())
            watchdog = self._watchdog
        errors = []
        for run in runs:
            if run.done:
                continue
            try:
                self._stop_run(run, TaskState.CANCELLED)
            except TransitionRejected:
                pass
            except Exception:
                errors.append("close_stop_failed")
        if watchdog is not None and watchdog is not current_thread():
            watchdog.join(timeout=3)
        if errors:
            raise TaskControlError("could not close all managed tasks")
