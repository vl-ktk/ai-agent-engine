"""7.10: peer-to-peer approved subtasks, without changing the parent claim.

This is an offline trusted adapter to the existing 7.3 task/team/grant
machinery. It does not dispatch models, grant rights, merge patches or accept
the parent task. TaskInbox persists the exchange as an idempotent child.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Callable

from .main_specialists import (
    SpecialistCandidate, TrustedStepRequirements, load_catalog,
    load_specialist_pin, load_team,
)
from .task_api import OwnerScope, TaskInbox, TaskNotFound, TaskSubmission
from .task_permissions import ProtectedAction
from .task_state import TaskState


_EXCHANGE_ID = re.compile(r"peer_[0-9a-f]{32}\Z")
_TITLE = "Approved peer contribution"


class PeerExchangeDenied(PermissionError):
    """Sanitized denial: never expose plans, workspace paths or task prompts."""


@dataclass(frozen=True, slots=True)
class PeerExchange:
    parent_task_id: str
    child_task_id: str
    original_agent: str
    peer_agent: str


@dataclass(frozen=True, slots=True)
class PeerResult:
    exchange: PeerExchange
    baseline_commit: str
    result_commit: str
    patch_sha256: str | None


def _read(inbox: TaskInbox, scope: OwnerScope, child_id: str) -> tuple[PeerExchange, dict]:
    child = inbox._scoped_record(child_id, scope)
    if child.submission.title != _TITLE or child.parent_task_id is None:
        raise PeerExchangeDenied("not_an_approved_peer_exchange")
    try:
        raw = child.submission.prompt
        meta = json.loads(raw)
        if (type(meta) is not dict or meta.get("kind") != "peer_v1"
                or type(meta.get("agent")) is not str
                or type(meta.get("provider")) is not str
                or type(meta.get("original")) is not str
                or type(meta.get("plan")) is not str
                or type(meta.get("team")) is not str
                or type(meta.get("step")) is not str
                or type(meta.get("catalog")) is not str
                or type(meta.get("skills")) is not list
                or type(meta.get("actions")) is not list):
            raise ValueError("invalid exchange marker")
    except (TypeError, ValueError, UnicodeError):
        raise PeerExchangeDenied("invalid_peer_exchange_receipt") from None
    parent = inbox._scoped_record(child.parent_task_id, scope)
    parent_pin = load_specialist_pin(inbox, scope, parent.task_id)
    if (parent.project_id != child.project_id or parent_pin is None
            or parent_pin.agent_id != meta["original"]
            or parent_pin.plan_digest != meta["plan"]
            or parent_pin.team_digest != meta["team"]
            or parent_pin.catalog_digest != meta["catalog"]
            or parent_pin.step_id != meta["step"]
            or parent_pin.agent_id == meta["agent"]):
        raise PeerExchangeDenied("peer_exchange_binding_changed")
    return (PeerExchange(parent.task_id, child.task_id,
                         parent_pin.agent_id, meta["agent"]), meta)


def offer_peer_subtask(inbox: TaskInbox, scope: OwnerScope, *,
                       exchange_id: str, requirements: TrustedStepRequirements,
                       team_id: str, candidate: SpecialistCandidate) -> PeerExchange:
    """Offer an approved step to a different member, not the parent assignment.

    The trusted coordinator provides a unique exchange_id. No grant is copied;
    assigning the child later requires ordinary 7.3 assign_specialist and its
    *own* exact task-scoped grants, including the chosen provider policy.
    """
    if (not isinstance(inbox, TaskInbox) or inbox._store is None
            or not isinstance(scope, OwnerScope)
            or not isinstance(requirements, TrustedStepRequirements)
            or not isinstance(candidate, SpecialistCandidate)
            or type(exchange_id) is not str or not _EXCHANGE_ID.fullmatch(exchange_id)):
        raise PeerExchangeDenied("invalid_peer_exchange_context")
    with inbox._lock:
        try:
            parent = inbox._scoped_record(requirements.task_id, scope)
        except TaskNotFound:
            raise PeerExchangeDenied("peer_exchange_not_approved") from None
        pin = load_specialist_pin(inbox, scope, parent.task_id)
        team = load_team(inbox, scope, team_id)
        if (pin is None or team is None
                or inbox._states[parent.task_id] not in
                (TaskState.ASSIGNED, TaskState.RUNNING)
                or inbox._store.emergency_held(scope.owner_id, parent.project_id)
                or inbox._store.transfer_has_started(parent.task_id)
                or inbox._store.crisis_child_state(parent.task_id) is not None
                or pin.plan_id != requirements.plan_id
                or pin.step_id != requirements.step_id
                or pin.team_id != team_id or pin.team_digest != team.proposal.sha256
                or parent.project_id != team.proposal.spec.project_id
                or pin.agent_id == candidate.agent_id):
            raise PeerExchangeDenied("peer_exchange_not_approved")
        catalog = load_catalog(inbox, scope, team.proposal.spec.catalog_revision)
        member = next((m for m in team.proposal.spec.members
                       if m.agent_id == candidate.agent_id), None)
        roles = (() if catalog is None else
                 (*catalog.proposal.spec.roles, *team.proposal.spec.local_roles))
        role = next((r for r in roles
                     if member is not None and r.role_id == member.role_id), None)
        original = inbox._assignments[parent.task_id]
        if (catalog is None or catalog.proposal.sha256 != pin.catalog_digest
                or member is None or role is None
                or member.workspace_name == original.workspace_name
                or not set(requirements.skills).issubset(role.skills)
                or not set(requirements.actions).issubset(member.actions)
                or (candidate.provider_id != role.primary_provider
                    and (candidate.provider_id not in role.fallback_providers
                         or candidate.fallback_reason is None))
                or (candidate.provider_id == role.primary_provider
                    and candidate.fallback_reason is not None)):
            raise PeerExchangeDenied("peer_candidate_not_approved")
        meta = {"kind":"peer_v1", "original":pin.agent_id,
                "agent":candidate.agent_id, "provider":candidate.provider_id,
                "fallback":candidate.fallback_reason,
                "plan":pin.plan_digest, "team":pin.team_digest,
                "catalog":pin.catalog_digest, "step":pin.step_id,
                "skills":list(requirements.skills),
                "actions":[action.value for action in requirements.actions]}
        child = inbox.accept(TaskSubmission(
            parent.project_id, _TITLE,
            json.dumps(meta, sort_keys=True, separators=(",", ":")),
            parent_task_id=parent.task_id), scope,
            idempotency_key="peer_v1:" + exchange_id)
        return _read(inbox, scope, child.task_id)[0]


def peer_requirements(inbox: TaskInbox, scope: OwnerScope,
                      child_task_id: str) -> TrustedStepRequirements:
    """Recover the pinned step from the durable child after a restart."""
    if not isinstance(inbox, TaskInbox) or inbox._store is None:
        raise PeerExchangeDenied("durable_peer_exchange_required")
    with inbox._lock:
        _exchange, meta = _read(inbox, scope, child_task_id)
        pin = load_specialist_pin(inbox, scope, _exchange.parent_task_id)
        try:
            return TrustedStepRequirements(child_task_id, pin.plan_id,
                                           meta["step"], tuple(meta["skills"]),
                                           tuple(ProtectedAction(x)
                                                 for x in meta["actions"]))
        except (ValueError, TypeError):
            raise PeerExchangeDenied("invalid_peer_exchange_receipt") from None


def verified_peer_result(inbox: TaskInbox, scope: OwnerScope,
                         child_task_id: str, *,
                         trusted_qa: Callable[[], bool]) -> PeerResult:
    """Return read-only verified provenance to the original worker for review.

    Caller owns independent QA and source validation; a successful child does
    not auto-merge, publish, change owner, or complete the parent.
    """
    if (not isinstance(inbox, TaskInbox) or inbox._store is None
            or not callable(trusted_qa)):
        raise PeerExchangeDenied("trusted_peer_qa_required")
    with inbox._lock:
        exchange, meta = _read(inbox, scope, child_task_id)
        child_pin = load_specialist_pin(inbox, scope, child_task_id)
        version = inbox._store.read_source_version(child_task_id)
        if (inbox._states[child_task_id] is not TaskState.SUCCEEDED
                or child_pin is None or version is None
                or child_pin.agent_id != exchange.peer_agent
                or child_pin.provider_id != meta["provider"]
                or child_pin.provider_reason != meta["fallback"]
                or child_pin.plan_digest != meta["plan"]
                or child_pin.team_digest != meta["team"]
                or child_pin.catalog_digest != meta["catalog"]
                or version[0] != exchange.peer_agent
                or (version[1] == version[2] and version[3] is None)):
            raise PeerExchangeDenied("unverified_peer_result")
        try:
            passed = trusted_qa() is True
        except Exception:
            passed = False
        if not passed:
            raise PeerExchangeDenied("peer_qa_failed")
        return PeerResult(exchange, version[1], version[2], version[3])
