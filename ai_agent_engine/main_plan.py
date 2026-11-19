"""Stage 7.2: bounded model decomposition, independent DAG validation and consent.

This is a library boundary, not an active GPT connector or execution engine.
The supplied model implements a provider-neutral protocol. Its text is
untrusted; it cannot choose an owner, grant permissions or approve a plan.
An authenticated owner gateway must construct OwnerDecision separately.
Approved plans are durable *documents*, not TaskInbox admissions. Stage 7.3
will map their steps to approved executors; no workers run in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Protocol
from uuid import uuid4

from .main_orchestrator import OwnerDecision
from .task_api import (InvalidTask, OwnerScope, ProjectForbidden, TaskInbox,
                       TaskSubmission, _PROPOSAL_ID, _PROJECT_ID,
                       parse_submission)
from .task_storage import StorageUnavailable


PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 131_072
MAX_STEPS = 32
MAX_DEPTH = 4
MAX_REPAIRS = 2  # Three drafts maximum, including the initial one.
MAX_EDGES = 32
_PLAN_ID = re.compile(r"plan_[0-9a-f]{32}\Z")
_STEP_ID = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_PLAN_FIELDS = frozenset({"schema_version", "steps"})
_STEP_FIELDS = frozenset({
    "step_id", "project_id", "title", "goal", "instructions", "boundaries",
    "expected_result", "acceptance_checks", "expected_artifacts",
    "depends_on", "parent_step_id",
})


class InvalidPlan(ValueError):
    """Untrusted plan rejected; reason is safe to show to an owner."""


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    project_id: str
    title: str
    goal: str
    instructions: str
    boundaries: str
    expected_result: str
    acceptance_checks: tuple[str, ...]
    expected_artifacts: tuple[str, ...]
    depends_on: tuple[str, ...]
    parent_step_id: str | None

    def payload(self) -> dict[str, object]:
        return {
            "step_id": self.step_id, "project_id": self.project_id,
            "title": self.title, "goal": self.goal,
            "instructions": self.instructions, "boundaries": self.boundaries,
            "expected_result": self.expected_result,
            "acceptance_checks": list(self.acceptance_checks),
            "expected_artifacts": list(self.expected_artifacts),
            "depends_on": list(self.depends_on),
            "parent_step_id": self.parent_step_id,
        }


@dataclass(frozen=True, slots=True)
class PlanSpec:
    """One owner/project-scoped, acyclic hierarchy and prerequisite graph."""

    steps: tuple[PlanStep, ...]

    def payload(self) -> dict[str, object]:
        return {"schema_version": PLAN_SCHEMA_VERSION,
                "steps": [step.payload() for step in self.steps]}

    def canonical_json(self) -> str:
        return _canonical(self.payload())

    def batches(self) -> tuple[tuple[str, ...], ...]:
        """Stable DAG layers: candidates only, NOT authorization to execute.

        Workspaces and ActionGrants must be checked later by the scheduler.
        Hierarchy is descriptive; only explicit depends_on creates an edge.
        """
        remaining = {step.step_id: set(step.depends_on) for step in self.steps}
        layers = []
        while remaining:
            ready = tuple(key for key in remaining if not remaining[key])
            if not ready:
                raise InvalidPlan("dependency cycle")
            layers.append(ready)
            for key in ready:
                del remaining[key]
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(layers)


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """Content-bound draft; it has no permission or execution side effects."""

    plan_id: str
    revision: int
    proposal_id: str
    owner_id: str
    source: TaskSubmission
    spec: PlanSpec
    sha256: str
    canonical_json: str


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    status: str  # ready | needs_owner
    attempts: int
    issue_codes: tuple[str, ...]
    proposal: PlanProposal | None


@dataclass(frozen=True, slots=True)
class ApprovedPlan:
    proposal: PlanProposal
    channel: str


class PlanningModel(Protocol):
    """Provider-neutral GPT boundary. Network/cost consent is caller's duty."""

    def propose(self, source: TaskSubmission, feedback: tuple[str, ...],
                detail_hint: str) -> bytes:
        """Return strict plan JSON. No owner IDs or claims of permission."""

    def review(self, source: TaskSubmission, plan_json: bytes) -> bytes:
        """Review this exact plan semantically; return strict review JSON."""


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidPlan("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise InvalidPlan("invalid JSON constant")


def _read_json(raw: bytes, *, maximum: int = MAX_PLAN_BYTES) -> object:
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise InvalidPlan("invalid plan body size")
    try:
        return json.loads(raw.decode("utf-8", "strict"),
                          object_pairs_hook=_unique_pairs,
                          parse_constant=_reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise InvalidPlan("invalid plan JSON") from None


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, UnicodeError):
        raise InvalidPlan("invalid plan text") from None


def _text(value: object, *, maximum: int) -> str:
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(ch) == 0 or ord(ch) == 127
                   or (ord(ch) < 32 and ch not in "\t\n") for ch in value)):
        raise InvalidPlan("invalid plan text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise InvalidPlan("invalid plan text") from None
    return value


def _text_list(value: object, *, limit: int) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= limit:
        raise InvalidPlan("missing or excessive plan checks or artifacts")
    result = tuple(_text(part, maximum=512) for part in value)
    if len(result) != len(set(result)):
        raise InvalidPlan("duplicate plan check or artifact")
    return result


def parse_plan(raw: bytes, scope: OwnerScope,
               source: TaskSubmission) -> PlanSpec:
    """Strict model JSON + trusted owner/project scope + nested DAG checks."""
    if not isinstance(scope, OwnerScope) or not isinstance(source, TaskSubmission):
        raise InvalidPlan("invalid trusted planning context")
    if source.project_id not in scope.project_ids:
        raise ProjectForbidden("project outside configured scope")
    obj = _read_json(raw)
    if (type(obj) is not dict or set(obj) != _PLAN_FIELDS
            or type(obj["schema_version"]) is not int
            or obj["schema_version"] != PLAN_SCHEMA_VERSION
            or type(obj["steps"]) is not list
            or not 1 <= len(obj["steps"]) <= MAX_STEPS):
        raise InvalidPlan("invalid plan schema or step count")
    steps = []
    for item in obj["steps"]:
        if type(item) is not dict or set(item) != _STEP_FIELDS:
            raise InvalidPlan("invalid subtask fields")
        step_id, project = item["step_id"], item["project_id"]
        if type(step_id) is not str or not _STEP_ID.fullmatch(step_id):
            raise InvalidPlan("invalid subtask identifier")
        if (type(project) is not str or not _PROJECT_ID.fullmatch(project)
                or project != source.project_id):
            raise InvalidPlan("subtask outside requested project")
        parent = item["parent_step_id"]
        if parent is not None and (type(parent) is not str
                                   or not _STEP_ID.fullmatch(parent)):
            raise InvalidPlan("invalid parent step")
        dependencies = item["depends_on"]
        if (type(dependencies) is not list or len(dependencies) > MAX_EDGES
                or any(type(dep) is not str or not _STEP_ID.fullmatch(dep)
                       for dep in dependencies)
                or len(dependencies) != len(set(dependencies))):
            raise InvalidPlan("invalid step dependencies")
        steps.append(PlanStep(
            step_id=step_id, project_id=project,
            title=_text(item["title"], maximum=256),
            goal=_text(item["goal"], maximum=2048),
            instructions=_text(item["instructions"], maximum=4096),
            boundaries=_text(item["boundaries"], maximum=2048),
            expected_result=_text(item["expected_result"], maximum=2048),
            acceptance_checks=_text_list(item["acceptance_checks"], limit=12),
            expected_artifacts=_text_list(item["expected_artifacts"], limit=12),
            depends_on=tuple(dependencies), parent_step_id=parent))

    by_id = {step.step_id: step for step in steps}
    if len(by_id) != len(steps):
        raise InvalidPlan("duplicate subtask identifier")
    for step in steps:
        if (step.parent_step_id == step.step_id
                or step.parent_step_id is not None
                and step.parent_step_id not in by_id):
            raise InvalidPlan("unknown or cyclic parent")
        if step.step_id in step.depends_on or any(
                dep not in by_id for dep in step.depends_on):
            raise InvalidPlan("unknown or self dependency")
    for step in steps:
        seen: set[str] = set()
        cursor: str | None = step.step_id
        while cursor is not None:
            if cursor in seen:
                raise InvalidPlan("parent hierarchy cycle")
            seen.add(cursor)
            if len(seen) > MAX_DEPTH:
                raise InvalidPlan("plan hierarchy too deep")
            cursor = by_id[cursor].parent_step_id
    spec = PlanSpec(tuple(steps))
    spec.batches()  # Raises on any dependency cycle.
    return spec


def _source_payload(source: TaskSubmission) -> dict[str, object]:
    obj: dict[str, object] = {
        "project_id": source.project_id, "title": source.title,
        "prompt": source.prompt, "priority": source.priority,
    }
    if source.parent_task_id is not None:
        obj["parent_task_id"] = source.parent_task_id
    return obj


def _proposal_json(plan_id: str, revision: int, source: TaskSubmission,
                   spec: PlanSpec) -> str:
    return _canonical({
        "plan_id": plan_id, "revision": revision,
        "source": _source_payload(source), "draft": spec.payload(),
    })


def _validate_identity(plan_id: str, revision: int) -> None:
    if (type(plan_id) is not str or not _PLAN_ID.fullmatch(plan_id)
            or type(revision) is not int or not 1 <= revision <= 1_000_000):
        raise InvalidPlan("invalid plan identity or revision")


def _make_proposal(scope: OwnerScope, source: TaskSubmission, spec: PlanSpec,
                   plan_id: str, revision: int,
                   proposal_id: str | None = None) -> PlanProposal:
    _validate_identity(plan_id, revision)
    canonical = _proposal_json(plan_id, revision, source, spec)
    if len(canonical.encode("utf-8")) > MAX_PLAN_BYTES:
        raise InvalidPlan("plan exceeds approved size limit")
    return PlanProposal(plan_id, revision, proposal_id or "proposal_" + uuid4().hex,
                        scope.owner_id, source, spec,
                        sha256(canonical.encode("utf-8")).hexdigest(), canonical)


def _check_review(raw: bytes, spec: PlanSpec) -> tuple[bool, tuple[str, ...]]:
    review = _read_json(raw, maximum=4096)
    if (type(review) is not dict
            or set(review) != {"plan_sha256", "passed", "summary", "findings"}
            or type(review["plan_sha256"]) is not str
            or review["plan_sha256"] != sha256(
                spec.canonical_json().encode("utf-8")).hexdigest()
            or type(review["passed"]) is not bool
            or type(review["findings"]) is not list
            or len(review["findings"]) > 12):
        raise InvalidPlan("invalid or mismatched semantic review")
    _text(review["summary"], maximum=1024)
    findings = tuple(_text(finding, maximum=256) for finding in review["findings"])
    if review["passed"] and review["findings"]:
        raise InvalidPlan("semantic review contradicts itself")
    return review["passed"] and not findings, findings


def decompose_for_review(model: PlanningModel, scope: OwnerScope,
                         source: TaskSubmission, *, detail_hint: str = "adaptive",
                         plan_id: str | None = None, revision: int = 1,
                         max_repairs: int = MAX_REPAIRS) -> PlanningOutcome:
    """Bounded model attempts; never approve, enqueue, execute or spend itself.

    The injected model adapter owns any provider calls/cost controls. After
    exhausting retries, return a sanitized owner escalation instead of a
    partially valid plan. Core schema always runs independently of GPT review.
    """
    if not isinstance(scope, OwnerScope) or not isinstance(source, TaskSubmission):
        raise InvalidPlan("invalid trusted planning context")
    if source.project_id not in scope.project_ids:
        raise ProjectForbidden("project outside configured scope")
    if detail_hint not in ("adaptive", "more", "less"):
        raise InvalidPlan("invalid detail preference")
    if type(max_repairs) is not int or not 0 <= max_repairs <= MAX_REPAIRS:
        raise InvalidPlan("invalid retry budget")
    plan_id = plan_id if plan_id is not None else "plan_" + uuid4().hex
    _validate_identity(plan_id, revision)
    feedback: tuple[str, ...] = ()
    for attempt in range(1, max_repairs + 2):
        try:
            raw = model.propose(source, feedback, detail_hint)
        except Exception:
            feedback = ("model_unavailable",)
            break  # Outage is not a validation error: fail closed immediately.
        try:
            spec = parse_plan(raw, scope, source)
            canonical = spec.canonical_json().encode("utf-8")
            review_raw = model.review(source, canonical)
            accepted, findings = _check_review(review_raw, spec)
            if not accepted:
                feedback = ("semantic_rejected", *findings[:3])
                continue
            proposal = _make_proposal(scope, source, spec, plan_id, revision)
            return PlanningOutcome("ready", attempt, (), proposal)
        except (InvalidPlan, ValueError, UnicodeError) as exc:
            # Parser errors are sanitized strings; never echo the model body.
            feedback = ("plan_validation_failed", str(exc)[:160])
        except Exception:
            feedback = ("model_unavailable",)
            break
    return PlanningOutcome("needs_owner", attempt, feedback, None)


def approve_plan(inbox: TaskInbox, scope: OwnerScope,
                 proposal: PlanProposal, decision: OwnerDecision) -> ApprovedPlan:
    """Authenticated explicit decision for the EXACT immutable plan revision.

    No execution permission or ActionGrant is granted. The plan is stored
    independently of TaskInbox queues. Owner authentication is external.
    """
    if (not isinstance(inbox, TaskInbox) or not isinstance(scope, OwnerScope)
            or not isinstance(proposal, PlanProposal)
            or not isinstance(decision, OwnerDecision)
            or type(decision.channel) is not str
            or decision.channel not in ("message", "button")
            or proposal.owner_id != scope.owner_id
            or decision.owner_id != scope.owner_id
            or decision.proposal_id != proposal.proposal_id
            or decision.proposal_sha256 != proposal.sha256
            or type(proposal.proposal_id) is not str
            or not _PROPOSAL_ID.fullmatch(proposal.proposal_id)):
        raise InvalidPlan("owner approval does not match plan")
    _validate_identity(proposal.plan_id, proposal.revision)
    if proposal.source.project_id not in scope.project_ids:
        raise ProjectForbidden("project outside configured scope")
    # Re-parse every field at the privilege boundary. A frozen dataclass is
    # not a cryptographic signature, and the model cannot be a trusted caller.
    source = parse_submission(_canonical(_source_payload(proposal.source)).encode("utf-8"))
    spec = parse_plan(proposal.spec.canonical_json().encode("utf-8"), scope, source)
    canonical = _proposal_json(proposal.plan_id, proposal.revision, source, spec)
    if (proposal.spec != spec or proposal.source != source
            or canonical != proposal.canonical_json
            or sha256(canonical.encode("utf-8")).hexdigest() != proposal.sha256):
        raise InvalidPlan("plan contents changed after review")
    inbox.persist_approved_plan(
        scope=scope, plan_id=proposal.plan_id, revision=proposal.revision,
        proposal_id=proposal.proposal_id, project_id=source.project_id,
        channel=decision.channel, plan_sha256=proposal.sha256,
        canonical_json=canonical)
    return ApprovedPlan(proposal, decision.channel)


def load_approved_plan(inbox: TaskInbox, scope: OwnerScope,
                       plan_id: str) -> ApprovedPlan | None:
    """Load only latest owner-scoped revision, verifying its saved SHA and DAG."""
    if not isinstance(inbox, TaskInbox) or not isinstance(scope, OwnerScope):
        raise InvalidPlan("invalid trusted planning context")
    if type(plan_id) is not str or not _PLAN_ID.fullmatch(plan_id):
        raise InvalidPlan("invalid plan identifier")
    row = inbox.latest_approved_plan(plan_id, scope)
    if row is None:
        return None
    saved_id, revision, proposal_id, owner_id, project_id, channel, digest, raw = row
    try:
        obj = _read_json(raw.encode("utf-8"))
        if (type(obj) is not dict
                or set(obj) != {"plan_id", "revision", "source", "draft"}
                or obj["plan_id"] != saved_id or obj["revision"] != revision):
            raise InvalidPlan("invalid saved plan")
        source = parse_submission(_canonical(obj["source"]).encode("utf-8"))
        if source.project_id != project_id:
            raise InvalidPlan("saved plan scope mismatch")
        spec = parse_plan(_canonical(obj["draft"]).encode("utf-8"), scope, source)
        proposal = _make_proposal(scope, source, spec, saved_id, revision, proposal_id)
        if (owner_id != scope.owner_id or proposal.canonical_json != raw
                or proposal.sha256 != digest):
            raise InvalidPlan("saved plan digest mismatch")
        return ApprovedPlan(proposal, channel)
    except (InvalidTask, TypeError, AttributeError, ValueError, UnicodeError) as exc:
        raise StorageUnavailable("approved plan storage failed validation") from exc
