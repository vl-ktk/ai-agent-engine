"""Stage 6.2: trusted GPT adapter for the durable task inbox.

The only production worker command is our private, single-request SIWC
subprocess. No HTTP execution endpoint, shell, API key fallback, agent tool
execution, automatic retry or provider-generated permission is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

from .task_api import OwnerScope, TaskInbox, TaskNotFound
from .task_assignment import AlreadyAssigned, validate_worker
from .task_control import TaskControlError, TaskProcessController
from .task_delivery import TaskDeliveryManager
from .task_isolation import provider_worker_environment, redact_text, sensitive_environment_values
from .task_permissions import ProtectedAction, TaskPermissionGate
from .task_state import TaskState, TransitionRejected


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
MAX_REPLY_BYTES = 100_000


class GPTTaskError(RuntimeError):
    """Sanitized failure, never containing user prompts or provider details."""


@dataclass(frozen=True)
class GPTTaskOutcome:
    task_id: str
    state: TaskState
    model_id: str
    executed: bool
    text: str | None = None


@dataclass(frozen=True)
class GPTProviderEvent:
    event_id: int
    task_id: str
    provider: str
    model_id: str
    outcome: str
    occurred_at: str


class GPTTaskExecutor:
    """Connect existing SIWC inference to scoped task state and delivery.

    An exact EXTERNAL_REQUEST grant from trusted server code is mandatory.
    Sending a request to a private subprocess happens *after* the audit
    commit, while the controller's task lock is held only for the short
    stdin write. The timeout watchdog remains able to stop a hung model.

    `worker_argv` is injection for offline fixture tests only. The trusted
    default never executes user-provided shell/CLI strings.
    """

    _provider = "gpt"

    def __init__(self, inbox: TaskInbox, scope: OwnerScope, *,
                 worker_id: str, model_id: str,
                 permission_gate: TaskPermissionGate,
                 timeout_seconds: float = 30,
                 worker_argv: tuple[str, ...] | None = None) -> None:
        if not isinstance(inbox, TaskInbox) or not inbox.durable:
            raise GPTTaskError("durable inbox is required")
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected trusted owner scope")
        validate_worker(worker_id)
        if type(model_id) is not str or not _MODEL.fullmatch(model_id):
            raise ValueError("invalid configured model")
        if (type(timeout_seconds) not in (float, int)
                or not 0.05 <= timeout_seconds <= 86400):
            raise ValueError("invalid model deadline")
        if (not isinstance(permission_gate, TaskPermissionGate)
                or permission_gate.inbox is not inbox):
            raise GPTTaskError("trusted permission gate required")
        if worker_argv is not None and (
                type(worker_argv) is not tuple or not worker_argv
                or any(type(s) is not str or not s or "\x00" in s
                       for s in worker_argv)):
            raise ValueError("invalid trusted worker command")
        self.inbox = inbox
        self.scope = scope
        self.worker_id = worker_id
        self.model_id = model_id
        self.timeout_seconds = float(timeout_seconds)
        self.worker_argv = (worker_argv or (
            sys.executable, "-m", "ai_agent_engine.gpt_task_worker"))
        self.delivery = TaskDeliveryManager(inbox, retry_delay_seconds=0)
        self.control = TaskProcessController(inbox, permission_gate=permission_gate)

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        """Provider subprocess sees only service essentials and its own login HOME."""
        return provider_worker_environment(
            os.environ, str(Path(__file__).resolve().parent.parent))

    @staticmethod
    def _stop_unattached(process: subprocess.Popen) -> None:
        try:
            if process.poll() is None and os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=3)

    def _claim_or_verify(self, task_id: str, workspace_name: str) -> None:
        assigned = self.inbox.get_assignment(task_id, self.scope)
        if assigned is None:
            try:
                assigned = self.inbox.claim_task(task_id, self.worker_id,
                                                 workspace_name, self.scope)
            except AlreadyAssigned:
                assigned = self.inbox.get_assignment(task_id, self.scope)
        if (assigned is None or assigned.worker_id != self.worker_id
                or assigned.workspace_name != workspace_name):
            raise GPTTaskError("task belongs to another trusted assignment")

    def _audit(self, task_id: str, outcome: str) -> None:
        with self.inbox._lock:
            self.inbox._scoped_record(task_id, self.scope)
            self.inbox._store.write_provider_audit(task_id, self.model_id,
                                                    outcome, provider=self._provider)

    def provider_history(self, task_id: str) -> tuple[GPTProviderEvent, ...]:
        """Scoped, private SQLite evidence: no payloads or model responses."""
        with self.inbox._lock:
            self.inbox._scoped_record(task_id, self.scope)
            return tuple(GPTProviderEvent(*row) for row
                         in self.inbox._store.provider_history(task_id))

    @staticmethod
    def _failure_category(output: bytes) -> str:
        if len(output) > 512:
            return "worker_failure"
        try:
            response = json.loads(output)
            if (type(response) is dict
                    and response.get("ok") is False
                    and response.get("error") in
                    ("quota", "auth", "unsupported", "unavailable")):
                return response["error"]
        except (ValueError, UnicodeError, TypeError):
            pass
        return "worker_failure"

    def execute(self, task_id: str, workspace_name: str) -> GPTTaskOutcome:
        """One delivery → one child → audited model call → durable outcome.

        A duplicate obtains no runnable ticket. Results are returned only to
        trusted calling Python; stage 6.12 owns durable artifact transfer.
        """
        record = self.inbox.get(task_id, self.scope)
        if record is None:
            raise TaskNotFound("task outside configured owner scope")
        prompt = record.submission.prompt
        if len(prompt) > 4000:
            raise GPTTaskError("task exceeds ChatGPT plan request limit")
        payload = json.dumps({"model_id": self.model_id, "prompt": prompt},
                             ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > 20_000:
            raise GPTTaskError("private task payload too large")
        self._claim_or_verify(task_id, workspace_name)
        ticket = self.delivery.reserve(task_id, self.scope,
                                       actor_id=self.worker_id, max_attempts=1)
        if not ticket.should_execute:
            state = self.inbox.state(task_id, self.scope)
            return GPTTaskOutcome(task_id, state, self.model_id, False)
        assignment = self.inbox.get_assignment(task_id, self.scope)
        try:
            # Stage 6.9: a private one-byte gate keeps the real worker from
            # executing until its PID identity is committed in SQLite.
            process = self.control.launch_gated(
                task_id, self.scope, actor_id=self.worker_id,
                worker_argv=self.worker_argv, cwd=assignment.workspace_dir,
                environment=self._worker_environment(),
                timeout_seconds=self.timeout_seconds)
        except (OSError, TaskControlError):
            if self.inbox.state(task_id, self.scope) is TaskState.ASSIGNED:
                self.delivery.report(task_id, self.scope, actor_id=self.worker_id,
                                     token=ticket.token, success=False)
                self._audit(task_id, "worker_failure")
                self.inbox.transition_state(task_id, TaskState.FAILED, self.scope,
                                            actor_id=self.worker_id)
            raise GPTTaskError("trusted GPT worker cannot start") from None

        try:
            def send_private_request() -> None:
                process.stdin.write(payload)
                process.stdin.flush()
                process.stdin.close()
                process.stdin = None

            self.control.execute_protected(
                task_id, self.scope, actor_id=self.worker_id,
                action=ProtectedAction.EXTERNAL_REQUEST,
                operation=send_private_request)
        except BaseException:
            # Nothing was sent on a refused grant. Even when SQLite fails,
            # the controller must stop the child before releasing its lease.
            try:
                self.control.cancel(task_id, self.scope)
            finally:
                process.communicate(timeout=3)
            raise

        output, _ = process.communicate()
        state = self.inbox.state(task_id, self.scope)
        if state in (TaskState.TIMED_OUT, TaskState.CANCELLED):
            self._audit(task_id, state.value)
            return GPTTaskOutcome(task_id, state, self.model_id, True)
        if state is not TaskState.RUNNING:
            raise GPTTaskError("task state changed while GPT was running")

        if process.returncode == 0:
            try:
                if len(output) > MAX_REPLY_BYTES:
                    raise ValueError("oversized private worker reply")
                result = json.loads(output.decode("utf-8", errors="strict"))
                if (not isinstance(result, dict)
                        or set(result) != {"ok", "text"}
                        or result["ok"] is not True
                        or type(result["text"]) is not str
                        or not result["text"].strip()
                        or len(result["text"]) > 20_000):
                    raise ValueError("invalid private worker reply")
            except (ValueError, UnicodeError):
                self._audit(task_id, "invalid_result")
                self.control.cancel(task_id, self.scope)
                raise GPTTaskError("trusted GPT worker returned invalid data") from None
            try:
                self._audit(task_id, "completed")
            except BaseException:
                self.control.cancel(task_id, self.scope)
                raise
            self.control.finish(task_id, self.scope, success=True)
            self.delivery.report(task_id, self.scope, actor_id=self.worker_id,
                                 token=ticket.token, success=True)
            return GPTTaskOutcome(
                task_id, TaskState.SUCCEEDED, self.model_id, True,
                redact_text(result["text"],
                            secrets=sensitive_environment_values(os.environ)))

        try:
            self.control.complete_failed_attempt(task_id, self.scope)
        except TransitionRejected:
            # Timeout watchdog may have committed TIMED_OUT between our
            # state read and attempt completion. Do not falsely mark FAILED.
            state = self.inbox.state(task_id, self.scope)
            if state in (TaskState.TIMED_OUT, TaskState.CANCELLED):
                self._audit(task_id, state.value)
                return GPTTaskOutcome(task_id, state, self.model_id, True)
            raise
        self._audit(task_id, self._failure_category(output))
        self.delivery.report(task_id, self.scope, actor_id=self.worker_id,
                             token=ticket.token, success=False)
        self.control.finalize_exhausted(task_id, self.scope)
        return GPTTaskOutcome(task_id, TaskState.FAILED, self.model_id, True)

    def close(self) -> None:
        self.control.close()
