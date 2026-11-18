"""Stage 6.4: provider-neutral task/context/role/permission request contract.

Only trusted coordinator code may build or bind an execution request. Parsed
JSON is DATA, not a source of identity, role authority or action grants.
Actual side effects still require TaskPermissionGate.run at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .core import AgentRole
from .task_api import (
    MAX_PRIORITY, MAX_PROMPT_CHARS, MAX_TITLE_CHARS, OwnerScope,
    TaskInbox, _PROJECT_ID, _TASK_ID,
)
from .task_assignment import _WORKER_ID, _WORKSPACE_NAME
from .task_permissions import ProtectedAction


REQUEST_VERSION = 1
MAX_REQUEST_BYTES = 65_536
MAX_CONTEXT_CHARS = 4_096
MAX_INSTRUCTIONS_CHARS = 4_096
_SHAPE = frozenset({"schema_version", "task", "context", "role", "permissions"})
_TASK = frozenset({"task_id", "project_id", "title", "prompt",
                   "parent_task_id", "priority"})
_CONTEXT = frozenset({"owner_id", "workspace_name", "summary"})
_ROLE = frozenset({"worker_id", "kind", "instructions"})
_PERMISSIONS = frozenset({"required_actions"})


class InvalidTaskRequest(ValueError):
    """No untrusted request details are reflected in errors."""


@dataclass(frozen=True, slots=True)
class TaskExecutionRequest:
    """Validated immutable data; permissions remain NON-AUTHORITATIVE."""

    task_id: str
    project_id: str
    title: str
    prompt: str
    parent_task_id: str | None
    priority: int
    owner_id: str
    workspace_name: str
    summary: str
    worker_id: str
    role: AgentRole
    instructions: str
    required_actions: tuple[ProtectedAction, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": REQUEST_VERSION,
            "task": {
                "task_id": self.task_id, "project_id": self.project_id,
                "title": self.title, "prompt": self.prompt,
                "parent_task_id": self.parent_task_id, "priority": self.priority,
            },
            "context": {
                "owner_id": self.owner_id, "workspace_name": self.workspace_name,
                "summary": self.summary,
            },
            "role": {
                "worker_id": self.worker_id, "kind": self.role.value,
                "instructions": self.instructions,
            },
            "permissions": {
                "required_actions": [a.value for a in self.required_actions],
            },
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_payload(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")


def _exact(obj: object, keys: frozenset[str]) -> dict:
    if type(obj) is not dict or set(obj) != keys:
        raise InvalidTaskRequest("invalid task request")
    return obj


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise InvalidTaskRequest("invalid task request")
    return value


def _text(value: object, limit: int, *, allow_blank: bool = True,
          no_control: bool = False) -> str:
    if (type(value) is not str or len(value) > limit
            or (not allow_blank and not value.strip())
            or "\x00" in value
            or (no_control and any(ord(c) < 32 or ord(c) == 127 for c in value))):
        raise InvalidTaskRequest("invalid task request")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise InvalidTaskRequest("invalid task request") from None
    return value


def _decode_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidTaskRequest("invalid task request")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise InvalidTaskRequest("invalid task request")


def parse_task_request(raw: bytes) -> TaskExecutionRequest:
    """Validate bounded strict JSON into data, without granting authority."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        raise InvalidTaskRequest("invalid task request")
    try:
        obj = json.loads(raw.decode("utf-8", errors="strict"),
                         object_pairs_hook=_decode_pairs,
                         parse_constant=_reject_constant)
        root = _exact(obj, _SHAPE)
        if type(root["schema_version"]) is not int or root["schema_version"] != REQUEST_VERSION:
            raise InvalidTaskRequest("invalid task request")
        task = _exact(root["task"], _TASK)
        context = _exact(root["context"], _CONTEXT)
        role = _exact(root["role"], _ROLE)
        permissions = _exact(root["permissions"], _PERMISSIONS)

        task_id = _identifier(task["task_id"], _TASK_ID)
        project_id = _identifier(task["project_id"], _PROJECT_ID)
        title = _text(task["title"], MAX_TITLE_CHARS, allow_blank=False,
                      no_control=True)
        prompt = _text(task["prompt"], MAX_PROMPT_CHARS, allow_blank=False)
        parent = task["parent_task_id"]
        if parent is not None:
            parent = _identifier(parent, _TASK_ID)
        if ((parent is None and not task_id.startswith("task_"))
                or (parent is not None and not task_id.startswith("subtask_"))):
            raise InvalidTaskRequest("invalid task request")
        priority = task["priority"]
        if type(priority) is not int or not 0 <= priority <= MAX_PRIORITY:
            raise InvalidTaskRequest("invalid task request")

        owner_id = _identifier(context["owner_id"], _PROJECT_ID)
        workspace = _identifier(context["workspace_name"], _WORKSPACE_NAME)
        summary = _text(context["summary"], MAX_CONTEXT_CHARS)
        worker_id = _identifier(role["worker_id"], _WORKER_ID)
        instructions = _text(role["instructions"], MAX_INSTRUCTIONS_CHARS)
        if type(role["kind"]) is not str:
            raise InvalidTaskRequest("invalid task request")
        kind = AgentRole(role["kind"])
        actions = permissions["required_actions"]
        if (type(actions) is not list or len(actions) > len(ProtectedAction)
                or any(type(a) is not str for a in actions)
                or actions != sorted(set(actions))):
            raise InvalidTaskRequest("invalid task request")
        required_actions = tuple(ProtectedAction(a) for a in actions)
        return TaskExecutionRequest(task_id, project_id, title, prompt, parent,
                                    priority, owner_id, workspace, summary,
                                    worker_id, kind, instructions, required_actions)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise InvalidTaskRequest("invalid task request") from None


def build_task_request(inbox: TaskInbox, scope: OwnerScope, task_id: str, *,
                       role: AgentRole, context_summary: str = "",
                       role_instructions: str = "",
                       required_actions: tuple[ProtectedAction, ...] = ()) -> TaskExecutionRequest:
    """Create a request only from trusted, already persisted identity/claim.

    `required_actions` is a declaration of *needed* actions from trusted
    orchestration code, NOT an ActionGrant or permission to execute them.
    """
    if (not isinstance(inbox, TaskInbox) or not isinstance(scope, OwnerScope)
            or not isinstance(role, AgentRole)
            or type(required_actions) is not tuple
            or any(not isinstance(a, ProtectedAction) for a in required_actions)):
        raise InvalidTaskRequest("invalid trusted request configuration")
    record = inbox.get(task_id, scope)
    if record is None:
        raise InvalidTaskRequest("task unavailable in owner scope")
    assignment = inbox.get_assignment(task_id, scope)
    if assignment is None or assignment.task_id != record.task_id:
        raise InvalidTaskRequest("task has no trusted assignment")
    payload = {
        "schema_version": REQUEST_VERSION,
        "task": {
            "task_id": record.task_id, "project_id": record.project_id,
            "title": record.submission.title, "prompt": record.submission.prompt,
            "parent_task_id": record.parent_task_id,
            "priority": record.submission.priority,
        },
        "context": {
            "owner_id": record.owner_id, "workspace_name": assignment.workspace_name,
            "summary": context_summary,
        },
        "role": {
            "worker_id": assignment.worker_id, "kind": role.value,
            "instructions": role_instructions,
        },
        "permissions": {"required_actions": sorted(a.value for a in required_actions)},
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeError):
        raise InvalidTaskRequest("invalid trusted request configuration") from None
    return parse_task_request(encoded)


def verify_bound_task_request(raw: bytes, inbox: TaskInbox, scope: OwnerScope,
                              task_id: str, *, role: AgentRole,
                              context_summary: str = "", role_instructions: str = "",
                              required_actions: tuple[ProtectedAction, ...] = ()) -> TaskExecutionRequest:
    """Reject JSON that disagrees with ANY trusted owner/task/role/policy fact.

    The subsequent TaskPermissionGate still makes the authoritative decision
    on each actual side effect, including expiration and task state.
    """
    candidate = parse_task_request(raw)
    expected = build_task_request(
        inbox, scope, task_id, role=role, context_summary=context_summary,
        role_instructions=role_instructions, required_actions=required_actions)
    if candidate != expected:
        raise InvalidTaskRequest("request does not match trusted task context")
    return candidate
