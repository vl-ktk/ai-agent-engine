"""Stage 7.1: trusted owner-scoped entry and bounded GPT context.

No provider calls, HTTP authentication, conversation UI or autonomous model
execution are supplied here. An authenticated owner adapter must explicitly
construct OwnerDecision; neither model text nor a parsed query can authorize
admission. The approved proposal is persisted atomically with its task.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import uuid4

from .task_api import (
    InvalidTask, OwnerApprovalProof, OwnerScope, ProjectForbidden,
    TaskInbox, TaskRecord, TaskSubmission, _PROJECT_ID, _TASK_ID,
    parse_submission,
)


CONTEXT_VERSION = 1
MAX_QUERY_BYTES = 4096
MAX_PROJECTS = 128
MAX_FOCUS_TASKS = 16


class InvalidOrchestratorRequest(ValueError):
    """Sanitized denial; never echo tasks, secrets or inaccessible project IDs."""


@dataclass(frozen=True, slots=True)
class ContextQuery:
    """Untrusted selection ONLY; owner, privileges and role are server-bound."""

    focus_project_id: str | None
    focus_task_id: str | None


@dataclass(frozen=True, slots=True)
class MainOrchestratorContext:
    """Provider-neutral immutable snapshot with no raw prompts or secrets."""

    owner_id: str
    projects: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    focus_project_id: str | None
    focus_tasks: tuple[tuple[str, str, int], ...]
    focus_truncated: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": CONTEXT_VERSION,
            "owner_id": self.owner_id,
            "projects": [
                {"project_id": project_id, "states": dict(states)}
                for project_id, states in self.projects
            ],
            "focus": None if self.focus_project_id is None else {
                "project_id": self.focus_project_id,
                "tasks": [{"task_id": task_id, "state": state,
                           "priority": priority}
                          for task_id, state, priority in self.focus_tasks],
                "truncated": self.focus_truncated,
            },
        }


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise InvalidOrchestratorRequest("invalid context query")
        obj[key] = value
    return obj


def parse_context_query(raw: bytes) -> ContextQuery:
    """Deny owner/role/grant overrides, duplicate keys and unknown fields."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_QUERY_BYTES:
        raise InvalidOrchestratorRequest("invalid context query")
    try:
        obj = json.loads(raw.decode("utf-8", "strict"),
                         object_pairs_hook=_unique_pairs)
        if (type(obj) is not dict or set(obj) != {
                "schema_version", "focus_project_id", "focus_task_id"}
                or type(obj["schema_version"]) is not int
                or obj["schema_version"] != CONTEXT_VERSION):
            raise InvalidOrchestratorRequest("invalid context query")
        project_id, task_id = obj["focus_project_id"], obj["focus_task_id"]
        if (project_id is not None and (type(project_id) is not str
                                         or not _PROJECT_ID.fullmatch(project_id))):
            raise InvalidOrchestratorRequest("invalid context query")
        if (task_id is not None and (type(task_id) is not str
                                      or not _TASK_ID.fullmatch(task_id))):
            raise InvalidOrchestratorRequest("invalid context query")
        if task_id is not None and project_id is None:
            raise InvalidOrchestratorRequest("invalid context query")
        return ContextQuery(project_id, task_id)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise InvalidOrchestratorRequest("invalid context query") from None


def build_main_context(inbox: TaskInbox, scope: OwnerScope,
                       query: ContextQuery) -> MainOrchestratorContext:
    """Global safe status counts + details only for one allowed project.

    No user-controlled title/prompt, direct workspace, OAuth token or
    cross-owner material can enter this snapshot. Model-provided IDs cannot
    expand trusted scope. Detail content requires a later explicit scoped API.
    """
    if (not isinstance(inbox, TaskInbox) or not isinstance(scope, OwnerScope)
            or not isinstance(query, ContextQuery)):
        raise InvalidOrchestratorRequest("invalid trusted context")
    if len(scope.project_ids) > MAX_PROJECTS:
        raise InvalidOrchestratorRequest("project scope too large")
    if (query.focus_project_id is not None
            and query.focus_project_id not in scope.project_ids):
        raise ProjectForbidden("project outside configured scope")
    if query.focus_task_id is not None and query.focus_project_id is None:
        raise InvalidOrchestratorRequest("invalid context query")

    records = inbox.scoped_state_snapshot(scope)
    grouped: dict[str, list[tuple[TaskRecord, str]]] = {
        project_id: [] for project_id in scope.project_ids}
    for record, state in records:
        grouped[record.project_id].append((record, state.value))

    projects = []
    for project_id in sorted(scope.project_ids):
        states = {"queued": 0, "assigned": 0, "running": 0,
                  "succeeded": 0, "failed": 0, "cancelled": 0, "timed_out": 0}
        for _, state in grouped[project_id]:
            states[state] += 1
        projects.append((project_id, tuple(sorted(states.items()))))

    focus = query.focus_project_id
    visible: list[tuple[TaskRecord, str]] = []
    truncated = False
    if focus is not None:
        all_records = grouped[focus]
        if query.focus_task_id is not None:
            visible = [(record, state) for record, state in all_records
                       if record.task_id == query.focus_task_id]
            if not visible:
                raise InvalidOrchestratorRequest("task unavailable in owner scope")
        else:
            truncated = len(all_records) > MAX_FOCUS_TASKS
            visible = all_records[-MAX_FOCUS_TASKS:]
    return MainOrchestratorContext(
        scope.owner_id, tuple(projects), focus,
        tuple((record.task_id, state, record.submission.priority)
              for record, state in visible),
        truncated)


def _submission_digest(submission: TaskSubmission) -> str:
    parsed = parse_submission(json.dumps({
        "project_id": submission.project_id,
        "title": submission.title,
        "prompt": submission.prompt,
        "parent_task_id": submission.parent_task_id,
        "priority": submission.priority,
    } if submission.parent_task_id is not None else {
        "project_id": submission.project_id,
        "title": submission.title,
        "prompt": submission.prompt,
        "priority": submission.priority,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    payload = (parsed.project_id, parsed.title, parsed.prompt,
               parsed.parent_task_id, parsed.priority)
    return sha256(json.dumps(payload, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MainTaskProposal:
    """Discussion draft: creation alone has NO effect on TaskInbox."""

    proposal_id: str
    owner_id: str
    submission: TaskSubmission
    sha256: str


@dataclass(frozen=True, slots=True)
class OwnerDecision:
    """Provided by a trusted, owner-authenticated caller, never an LLM.

    The future panel/chat adapter must authenticate the owner, display/bind
    the exact proposal revision, and construct this only on explicit consent.
    """

    owner_id: str
    proposal_id: str
    proposal_sha256: str
    channel: str


def draft_main_task(scope: OwnerScope, submission: TaskSubmission) -> MainTaskProposal:
    if not isinstance(scope, OwnerScope) or not isinstance(submission, TaskSubmission):
        raise InvalidOrchestratorRequest("invalid trusted proposal")
    if submission.project_id not in scope.project_ids:
        raise ProjectForbidden("project outside configured scope")
    return MainTaskProposal("proposal_" + uuid4().hex, scope.owner_id,
                            submission, _submission_digest(submission))


def admit_approved_main_task(inbox: TaskInbox, scope: OwnerScope,
                             proposal: MainTaskProposal,
                             decision: OwnerDecision) -> TaskRecord:
    """Trusted bridge: atomic durable approval+task receipt, no model trigger.

    The owner authentication event must originate outside this module.
    Request text or a model's declaration of consent is never such an event.
    """
    if (not isinstance(inbox, TaskInbox) or not isinstance(scope, OwnerScope)
            or not isinstance(proposal, MainTaskProposal)
            or not isinstance(decision, OwnerDecision)
            or decision.channel not in ("message", "button")
            or type(decision.channel) is not str
            or proposal.owner_id != scope.owner_id
            or decision.owner_id != scope.owner_id
            or decision.proposal_id != proposal.proposal_id
            or decision.proposal_sha256 != proposal.sha256
            or _submission_digest(proposal.submission) != proposal.sha256):
        raise InvalidOrchestratorRequest("owner approval does not match proposal")
    if proposal.submission.project_id not in scope.project_ids:
        raise ProjectForbidden("project outside configured scope")
    proof = OwnerApprovalProof(proposal.proposal_id, decision.channel,
                               proposal.sha256)
    return inbox.accept(proposal.submission, scope,
                        idempotency_key=proposal.proposal_id,
                        owner_approval=proof)
