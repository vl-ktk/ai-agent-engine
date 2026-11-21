"""WEB-12: offline TaskInbox -> scoped mock ChatGPT Web -> task response.

This trusted one-shot bridge has no browser driver, background scheduler,
delivery ACK, retry loop or new permission issuer. It deliberately keeps a
RUNNING task and its capacity reservation on an uncertain model outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from .chatgpt_web_models import WebAgentModelRegistry
from .chatgpt_web_rotation import WebRotationReceipt
from .chatgpt_web_sessions import WebAgentSessionRegistry
from .core import AgentResult
from .main_shared_capacity import SharedCapacity
from .main_specialists import load_specialist_pin
from .task_api import OwnerScope, TaskInbox
from .task_permissions import ProtectedAction, TaskPermissionGate
from .task_request import verify_bound_task_request
from .task_response import (
    MAX_TEXT_CHARS, RESPONSE_VERSION, TaskExecutionResponse,
    parse_task_response,
)
from .task_state import TaskState


class WebCoreTaskError(RuntimeError):
    """Sanitized refusal; never includes prompts, outputs or credentials."""


@dataclass(frozen=True, slots=True)
class WebCoreTaskOutcome:
    task_id: str
    admitted: bool
    reason: str
    response: TaskExecutionResponse | None


class WebCoreTaskAdapter:
    """Use existing approved task/pin/permission/capacity and v1 contracts.

    WEB-13 will add persisted delivery identity/ACK and recovery. Until then,
    the first uncertain or interrupted call cannot be replayed automatically.
    """

    def __init__(self, inbox: TaskInbox, scope: OwnerScope,
                 sessions: WebAgentSessionRegistry,
                 models: WebAgentModelRegistry,
                 gate: TaskPermissionGate, capacity: SharedCapacity):
        if (not isinstance(inbox, TaskInbox) or inbox._store is None
                or not isinstance(scope, OwnerScope)
                or not isinstance(sessions, WebAgentSessionRegistry)
                or not isinstance(models, WebAgentModelRegistry)
                or models._sessions is not sessions
                or models._instruction_bootstrap is None
                or not isinstance(gate, TaskPermissionGate)
                or gate.inbox is not inbox
                or not isinstance(capacity, SharedCapacity)
                or capacity.inbox is not inbox):
            raise ValueError("trusted durable web task dependencies required")
        self.inbox = inbox
        self.scope = scope
        self.sessions = sessions
        self.models = models
        self.gate = gate
        self.capacity = capacity

    def execute(self, raw_request: bytes, *, task_id: str,
                worker_id: str, requested_model_id: str | None = None,
                requested_reasoning_effort: str | None = None,
                handoff_receipt: WebRotationReceipt | None = None
                ) -> WebCoreTaskOutcome:
        """Dispatch at most one local attempt; return only verified response.

        The caller supplies the v1 request as data; all authoritative fields
        are reconstructed from the persisted inbox/assignment and trusted
        agent registration. The task's role instructions come from WEB-08,
        never from this request. A capacity denial leaves it ASSIGNED.
        """
        try:
            record = self.inbox.get(task_id, self.scope)
            if record is None or record.project_id not in self.scope.project_ids:
                raise WebCoreTaskError("task outside trusted scope")
            identity = self.sessions.identity(
                owner_id=self.scope.owner_id, project_id=record.project_id,
                agent_id=worker_id)
            request = verify_bound_task_request(
                raw_request, self.inbox, self.scope, task_id,
                role=identity.role, context_summary="", role_instructions="",
                required_actions=(ProtectedAction.EXTERNAL_REQUEST,))
            if (request.worker_id != worker_id
                    or request.owner_id != identity.owner_id
                    or request.project_id != identity.project_id
                    or request.task_id != task_id):
                raise WebCoreTaskError("web task identity mismatch")
            if (handoff_receipt is not None
                    and (type(handoff_receipt) is not WebRotationReceipt
                         or (handoff_receipt.owner_id, handoff_receipt.project_id,
                             handoff_receipt.agent_id, handoff_receipt.task_id) !=
                            (request.owner_id, request.project_id,
                             worker_id, task_id))):
                raise WebCoreTaskError("handoff does not match bound task")
            pin = load_specialist_pin(self.inbox, self.scope, task_id)
            if (pin is None or pin.agent_id != worker_id
                    or pin.provider_id != "chatgpt_web"
                    or ProtectedAction.EXTERNAL_REQUEST not in pin.allowed_actions):
                raise WebCoreTaskError("unapproved web task provider")
            model_id = self.models.selected_model(
                owner_id=self.scope.owner_id, project_id=record.project_id,
                agent_id=worker_id, requested_model_id=requested_model_id)
            decision = self.capacity.acquire(
                task_id, self.scope, worker_id=worker_id)
        except Exception:
            raise WebCoreTaskError("web task admission unverified") from None

        if not decision.admitted:
            return WebCoreTaskOutcome(task_id, False, decision.reason, None)

        try:
            self.inbox.transition_state(
                task_id, TaskState.RUNNING, self.scope, actor_id=worker_id)

            def send() -> AgentResult:
                return self.models.run(
                    owner_id=self.scope.owner_id, project_id=record.project_id,
                    agent_id=worker_id, prompt=request.prompt,
                    requested_model_id=model_id,
                    requested_reasoning_effort=requested_reasoning_effort,
                    handoff_receipt=handoff_receipt)

            result = self.gate.run(
                task_id, self.scope, actor_id=worker_id,
                action=ProtectedAction.EXTERNAL_REQUEST, operation=send)
            if (type(result) is not AgentResult or result.status != "done"
                    or type(result.answer) is not str or not result.answer.strip()
                    or len(result.answer) > MAX_TEXT_CHARS
                    or result.steps != 1 or len(result.audit) != 1
                    or result.audit[0].event != "completed"
                    or result.audit[0].project_id != record.project_id
                    or result.audit[0].agent_id != worker_id):
                raise WebCoreTaskError("web task response unverified")
            # Existing strict provider-neutral response contract is validated
            # BEFORE a durable SUCCEEDED transition. No tool/file claims.
            payload = {
                "schema_version": RESPONSE_VERSION,
                "task_id": task_id, "provider": "chatgpt_web",
                "model_id": model_id, "state": TaskState.SUCCEEDED.value,
                "result": {"text": result.answer, "artifacts": []},
                "error": None,
                "limits": {"timeout_ms": 30_000,
                           "max_text_chars": MAX_TEXT_CHARS,
                           "text_chars": len(result.answer),
                           "token_usage": None},
            }
            response = parse_task_response(json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"))
            if response.task_id != task_id or response.provider != pin.provider_id:
                raise WebCoreTaskError("web response scope mismatch")
            self.inbox.transition_state(
                task_id, TaskState.SUCCEEDED, self.scope, actor_id=worker_id)
            self.capacity.release(
                task_id, self.scope, worker_id=worker_id,
                reservation_id=decision.reservation_id)
            return WebCoreTaskOutcome(task_id, True, "completed", response)
        except Exception:
            # A browser callback could have had an effect before failing.
            # Never release its reservation, change RUNNING to FAILED or
            # silently retry. WEB-13/15 must resolve uncertain delivery.
            raise WebCoreTaskError("web task outcome uncertain; recovery required") from None
