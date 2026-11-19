"""Stage 7.3: approved specialist profiles, project teams and grant-safe matching.

This is an offline, provider-neutral library. Raw proposals and candidates are
untrusted; owner consent, step requirements and ActionGrants must come from
separate authenticated/trusted callers. No model calls or task dispatch occur.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
import time
from typing import Callable, TypeVar
from uuid import uuid4

from .main_orchestrator import OwnerDecision
from .main_plan import load_approved_plan
from .task_api import (OwnerScope, ProjectForbidden, TaskInbox, _PROJECT_ID,
                       _PROPOSAL_ID, _TASK_ID)
from .task_assignment import validate_worker, validate_workspace_name
from .task_permissions import (ActionGrant, PermissionDenied, ProtectedAction,
                               TaskPermissionGate)
from .task_state import TaskState
from .task_storage import StorageUnavailable


VERSION = 1
MAX_BODY = 131_072
MAX_ROLES = 128
MAX_MEMBERS = 64
_ROLE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_TEAM_ID = re.compile(r"team_[a-z0-9_]{1,64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_ID = re.compile(r"plan_[0-9a-f]{32}\Z")
_WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_T = TypeVar("_T")


class InvalidSpecialist(ValueError):
    """A proposed profile or assignment is invalid; never echo private text."""


class SpecialistDenied(PermissionError):
    """Sanitized failed match; no worker, prompts or grants disclosed."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSpecialist("duplicate specialist JSON field")
        result[key] = value
    return result


def _read(raw: bytes) -> dict:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BODY:
        raise InvalidSpecialist("invalid specialist body")
    try:
        obj = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique,
                         parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError):
        raise InvalidSpecialist("invalid specialist JSON") from None
    if type(obj) is not dict:
        raise InvalidSpecialist("invalid specialist object")
    return obj


def _fields(obj: object, expected: set[str]) -> dict:
    if type(obj) is not dict or set(obj) != expected:
        raise InvalidSpecialist("invalid specialist fields")
    return obj


def _id(value: object, pattern: re.Pattern) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise InvalidSpecialist("invalid specialist identifier")
    return value


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise InvalidSpecialist("invalid specialist revision")
    return value


def _text(value: object, maximum: int) -> str:
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(ch) < 32 and ch not in "\n\t" for ch in value)):
        raise InvalidSpecialist("invalid specialist text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise InvalidSpecialist("invalid specialist text") from None
    return value


def _tokens(value: object, pattern: re.Pattern, maximum: int,
            *, allow_empty: bool = False) -> tuple[str, ...]:
    if (type(value) is not list or len(value) > maximum
            or (not allow_empty and not value)):
        raise InvalidSpecialist("invalid specialist list")
    entries = tuple(_id(item, pattern) for item in value)
    if len(set(entries)) != len(entries):
        raise InvalidSpecialist("duplicate specialist list entry")
    return entries


def _actions(value: object, *, allow_empty: bool = True) -> tuple[ProtectedAction, ...]:
    keys = _tokens(value, _ROLE, 5, allow_empty=allow_empty)
    try:
        return tuple(ProtectedAction(item) for item in keys)
    except ValueError:
        raise InvalidSpecialist("unknown protected action") from None


@dataclass(frozen=True, slots=True)
class RoleProfile:
    role_id: str
    name: str
    instructions: str
    boundaries: str
    skills: tuple[str, ...]
    tools: tuple[str, ...]
    actions: tuple[ProtectedAction, ...]
    primary_provider: str
    fallback_providers: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {"role_id": self.role_id, "name": self.name,
                "instructions": self.instructions, "boundaries": self.boundaries,
                "skills": list(self.skills), "tools": list(self.tools),
                "actions": [a.value for a in self.actions],
                "primary_provider": self.primary_provider,
                "fallback_providers": list(self.fallback_providers)}


def _role(obj: object) -> RoleProfile:
    o = _fields(obj, {"role_id", "name", "instructions", "boundaries",
                      "skills", "tools", "actions", "primary_provider",
                      "fallback_providers"})
    primary = _id(o["primary_provider"], _PROVIDER)
    alternatives = _tokens(o["fallback_providers"], _PROVIDER, 8,
                           allow_empty=True)
    if primary in alternatives:
        raise InvalidSpecialist("primary provider repeated as fallback")
    return RoleProfile(
        _id(o["role_id"], _ROLE), _text(o["name"], 128),
        _text(o["instructions"], 4096), _text(o["boundaries"], 2048),
        _tokens(o["skills"], _ROLE, 32),
        _tokens(o["tools"], _ROLE, 32, allow_empty=True),
        _actions(o["actions"]), primary, alternatives)


@dataclass(frozen=True, slots=True)
class CatalogSpec:
    roles: tuple[RoleProfile, ...]

    def payload(self) -> dict[str, object]:
        return {"schema_version": VERSION,
                "roles": [role.payload() for role in self.roles]}


def parse_catalog(raw: bytes) -> CatalogSpec:
    obj = _fields(_read(raw), {"schema_version", "roles"})
    if (type(obj["schema_version"]) is not int or obj["schema_version"] != VERSION
            or type(obj["roles"]) is not list
            or not 1 <= len(obj["roles"]) <= MAX_ROLES):
        raise InvalidSpecialist("invalid catalog version or size")
    roles = tuple(_role(r) for r in obj["roles"])
    if len(set(r.role_id for r in roles)) != len(roles):
        raise InvalidSpecialist("duplicate specialist role")
    return CatalogSpec(roles)


@dataclass(frozen=True, slots=True)
class TeamMember:
    agent_id: str
    role_id: str
    workspace_name: str
    actions: tuple[ProtectedAction, ...]

    def payload(self) -> dict[str, object]:
        return {"agent_id": self.agent_id, "role_id": self.role_id,
                "workspace_name": self.workspace_name,
                "actions": [action.value for action in self.actions]}


@dataclass(frozen=True, slots=True)
class TeamSpec:
    project_id: str
    catalog_revision: int
    catalog_digest: str
    local_roles: tuple[RoleProfile, ...]
    members: tuple[TeamMember, ...]
    role_limits: tuple[tuple[str, int], ...]

    def payload(self) -> dict[str, object]:
        return {"schema_version": VERSION, "project_id": self.project_id,
                "catalog_revision": self.catalog_revision,
                "catalog_sha256": self.catalog_digest,
                "local_roles": [role.payload() for role in self.local_roles],
                "members": [member.payload() for member in self.members],
                "role_limits": [{"role_id": role, "max_instances": limit}
                                for role,limit in self.role_limits]}


def parse_team(raw: bytes, scope: OwnerScope, catalog: CatalogSpec,
               catalog_revision: int, catalog_digest: str) -> TeamSpec:
    if not isinstance(scope, OwnerScope) or not isinstance(catalog, CatalogSpec):
        raise InvalidSpecialist("trusted catalog and scope required")
    obj = _fields(_read(raw), {"schema_version", "project_id",
                                "catalog_revision", "catalog_sha256",
                                "local_roles", "members", "role_limits"})
    if type(obj["schema_version"]) is not int or obj["schema_version"] != VERSION:
        raise InvalidSpecialist("invalid team schema")
    project = _id(obj["project_id"], _PROJECT_ID)
    if project not in scope.project_ids:
        raise ProjectForbidden("project outside configured scope")
    if (obj["catalog_revision"] != catalog_revision
            or type(obj["catalog_revision"]) is not int
            or obj["catalog_sha256"] != catalog_digest
            or type(obj["catalog_sha256"]) is not str):
        raise InvalidSpecialist("catalog version mismatch")
    if type(obj["local_roles"]) is not list or len(obj["local_roles"]) > 32:
        raise InvalidSpecialist("invalid local profiles")
    local = tuple(_role(o) for o in obj["local_roles"])
    roles = {r.role_id: r for r in catalog.roles}
    for profile in local:
        if profile.role_id in roles:
            raise InvalidSpecialist("project role shadows approved catalog")
        roles[profile.role_id] = profile
    if (type(obj["members"]) is not list
            or not 1 <= len(obj["members"]) <= MAX_MEMBERS):
        raise InvalidSpecialist("invalid project team size")
    members: list[TeamMember] = []
    for item in obj["members"]:
        m = _fields(item, {"agent_id", "role_id", "workspace_name", "actions"})
        agent = _id(m["agent_id"], _PROJECT_ID)
        role_id = _id(m["role_id"], _ROLE)
        if role_id not in roles:
            raise InvalidSpecialist("unapproved project role")
        workspace = _id(m["workspace_name"], _WORKSPACE)
        validate_worker(agent)
        validate_workspace_name(workspace)
        actions = _actions(m["actions"])
        if not set(actions).issubset(roles[role_id].actions):
            raise InvalidSpecialist("project role expands global action policy")
        members.append(TeamMember(agent, role_id, workspace, actions))
    if (len(set(m.agent_id for m in members)) != len(members)
            or len(set(m.workspace_name for m in members)) != len(members)):
        raise InvalidSpecialist("duplicate agent or shared workspace")
    if type(obj["role_limits"]) is not list or len(obj["role_limits"]) > MAX_MEMBERS:
        raise InvalidSpecialist("invalid role capacity")
    limits = []
    for value in obj["role_limits"]:
        item = _fields(value, {"role_id", "max_instances"})
        role = _id(item["role_id"], _ROLE)
        maximum = item["max_instances"]
        if (role not in roles or type(maximum) is not int
                or not 1 <= maximum <= MAX_MEMBERS):
            raise InvalidSpecialist("invalid role capacity")
        limits.append((role, maximum))
    if (len(set(r for r,_ in limits)) != len(limits)
            or {r for r,_ in limits} != {m.role_id for m in members}):
        raise InvalidSpecialist("team capacity list mismatch")
    for role,max_instances in limits:
        if sum(m.role_id == role for m in members) > max_instances:
            raise InvalidSpecialist("role exceeds approved team capacity")
    return TeamSpec(project, catalog_revision, catalog_digest,
                    local, tuple(members), tuple(limits))


@dataclass(frozen=True, slots=True)
class SpecialistProposal:
    kind: str
    owner_id: str
    project_id: str | None
    team_id: str | None
    revision: int
    proposal_id: str
    spec: CatalogSpec | TeamSpec
    canonical_json: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ApprovedSpecialists:
    proposal: SpecialistProposal
    channel: str


def _draft(kind: str, scope: OwnerScope, spec: CatalogSpec | TeamSpec,
           revision: int, team_id: str | None,
           proposal_id: str | None = None) -> SpecialistProposal:
    _revision(revision)
    body = {"kind": kind, "owner_id": scope.owner_id,
            "team_id": team_id, "revision": revision, "spec": spec.payload()}
    canonical = _json(body)
    if len(canonical.encode("utf-8")) > MAX_BODY:
        raise InvalidSpecialist("specialist proposal too large")
    return SpecialistProposal(kind, scope.owner_id,
                              spec.project_id if isinstance(spec, TeamSpec) else None,
                              team_id, revision,
                              proposal_id or "proposal_" + uuid4().hex,
                              spec, canonical, sha256(canonical.encode()).hexdigest())


def draft_catalog(scope: OwnerScope, raw: bytes, *, revision: int = 1) -> SpecialistProposal:
    if not isinstance(scope, OwnerScope):
        raise InvalidSpecialist("trusted owner scope required")
    return _draft("catalog", scope, parse_catalog(raw), revision, None)


def draft_team(scope: OwnerScope, raw: bytes, catalog: ApprovedSpecialists,
               *, team_id: str, revision: int = 1) -> SpecialistProposal:
    _id(team_id, _TEAM_ID)
    if (not isinstance(catalog, ApprovedSpecialists)
            or catalog.proposal.kind != "catalog"
            or catalog.proposal.owner_id != scope.owner_id):
        raise InvalidSpecialist("untrusted catalog for project team")
    spec = parse_team(raw, scope, catalog.proposal.spec,
                      catalog.proposal.revision, catalog.proposal.sha256)
    return _draft("team", scope, spec, revision, team_id)


def _approval(inbox: TaskInbox, scope: OwnerScope,
              proposal: SpecialistProposal, decision: OwnerDecision) -> ApprovedSpecialists:
    if (not isinstance(inbox, TaskInbox) or inbox._store is None
            or not isinstance(scope, OwnerScope)
            or not isinstance(proposal, SpecialistProposal)
            or not isinstance(decision, OwnerDecision)
            or type(decision.channel) is not str
            or decision.channel not in ("message", "button")
            or decision.owner_id != scope.owner_id
            or proposal.owner_id != scope.owner_id
            or decision.proposal_id != proposal.proposal_id
            or decision.proposal_sha256 != proposal.sha256
            or type(proposal.proposal_id) is not str
            or not _PROPOSAL_ID.fullmatch(proposal.proposal_id)):
        raise InvalidSpecialist("owner approval does not match specialist proposal")
    if proposal.kind == "catalog":
        spec = parse_catalog(_json(proposal.spec.payload()).encode())
        if proposal.team_id is not None or proposal.project_id is not None:
            raise InvalidSpecialist("invalid catalog scope")
    elif proposal.kind == "team":
        _id(proposal.team_id, _TEAM_ID)
        if proposal.project_id not in scope.project_ids:
            raise ProjectForbidden("project outside configured scope")
        with inbox._lock:
            row = inbox._store.specialist_catalog(scope.owner_id,
                                                   proposal.spec.catalog_revision)
            latest = inbox._store.specialist_catalog(scope.owner_id)
        catalog = _load_catalog_row(scope, row)
        if (catalog is None or latest is None
                or catalog.proposal.sha256 != proposal.spec.catalog_digest):
            raise InvalidSpecialist("team refers to unapproved catalog")
        spec = parse_team(_json(proposal.spec.payload()).encode(), scope,
                          catalog.proposal.spec, catalog.proposal.revision,
                          catalog.proposal.sha256)
    else:
        raise InvalidSpecialist("unknown specialist proposal kind")
    check = _draft(proposal.kind, scope, spec, proposal.revision,
                   proposal.team_id, proposal.proposal_id)
    if check != proposal:
        raise InvalidSpecialist("specialist proposal changed after review")
    with inbox._lock:
        inbox._store.save_specialist_approval(
            kind=proposal.kind, key=proposal.team_id or scope.owner_id,
            revision=proposal.revision, proposal_id=proposal.proposal_id,
            owner_id=scope.owner_id, project_id=proposal.project_id,
            channel=decision.channel, digest=proposal.sha256,
            canonical_json=proposal.canonical_json)
    return ApprovedSpecialists(proposal, decision.channel)


def approve_catalog(inbox: TaskInbox, scope: OwnerScope,
                    proposal: SpecialistProposal,
                    decision: OwnerDecision) -> ApprovedSpecialists:
    if proposal.kind != "catalog":
        raise InvalidSpecialist("expected catalog proposal")
    return _approval(inbox, scope, proposal, decision)


def approve_team(inbox: TaskInbox, scope: OwnerScope,
                 proposal: SpecialistProposal,
                 decision: OwnerDecision) -> ApprovedSpecialists:
    if proposal.kind != "team":
        raise InvalidSpecialist("expected project team proposal")
    return _approval(inbox, scope, proposal, decision)


def _load_catalog_row(scope: OwnerScope, row: tuple | None) -> ApprovedSpecialists | None:
    if row is None or row[0] != scope.owner_id:
        return None
    owner,revision,proposal_id,channel,digest,raw = row
    try:
        obj = _read(raw.encode())
        spec = parse_catalog(_json(obj["spec"]).encode())
        proposal = _draft("catalog", scope, spec, revision, None, proposal_id)
        if (obj != json.loads(proposal.canonical_json)
                or raw != proposal.canonical_json or digest != proposal.sha256):
            raise InvalidSpecialist("modified approved catalog")
        return ApprovedSpecialists(proposal,channel)
    except (InvalidSpecialist,KeyError,TypeError,ValueError) as exc:
        raise StorageUnavailable("approved catalog integrity failure") from exc


def load_catalog(inbox: TaskInbox, scope: OwnerScope,
                 revision: int | None = None) -> ApprovedSpecialists | None:
    if not isinstance(inbox, TaskInbox) or inbox._store is None or not isinstance(scope, OwnerScope):
        raise InvalidSpecialist("trusted catalog store and owner scope required")
    if revision is not None:
        _revision(revision)
    with inbox._lock:
        row = inbox._store.specialist_catalog(scope.owner_id, revision)
    return _load_catalog_row(scope, row)


def _load_team_row(scope: OwnerScope, row: tuple | None,
                   catalog: ApprovedSpecialists | None) -> ApprovedSpecialists | None:
    if row is None or row[3] != scope.owner_id or row[4] not in scope.project_ids:
        return None
    if catalog is None:
        raise StorageUnavailable("approved team refers to missing catalog")
    team_id,revision,proposal_id,owner,project,channel,digest,raw = row
    try:
        obj = _read(raw.encode())
        spec = parse_team(_json(obj["spec"]).encode(), scope,
                          catalog.proposal.spec, catalog.proposal.revision,
                          catalog.proposal.sha256)
        proposal = _draft("team",scope,spec,revision,team_id,proposal_id)
        if (obj != json.loads(proposal.canonical_json)
                or raw != proposal.canonical_json or digest != proposal.sha256
                or spec.project_id != project):
            raise InvalidSpecialist("modified approved team")
        return ApprovedSpecialists(proposal,channel)
    except (InvalidSpecialist,KeyError,TypeError,ValueError) as exc:
        raise StorageUnavailable("approved team integrity failure") from exc


def load_team(inbox: TaskInbox, scope: OwnerScope, team_id: str,
              revision: int | None = None) -> ApprovedSpecialists | None:
    _id(team_id, _TEAM_ID)
    if not isinstance(inbox, TaskInbox) or inbox._store is None or not isinstance(scope, OwnerScope):
        raise InvalidSpecialist("trusted team store and owner scope required")
    if revision is not None:
        _revision(revision)
    with inbox._lock:
        row = inbox._store.specialist_team(team_id,revision)
    if row is None or row[3] != scope.owner_id or row[4] not in scope.project_ids:
        return None
    try:
        obj = _read(row[7].encode())
        catalog_revision = _revision(obj["spec"]["catalog_revision"])
    except (InvalidSpecialist,KeyError,TypeError,ValueError) as exc:
        raise StorageUnavailable("approved team integrity failure") from exc
    catalog = load_catalog(inbox,scope,catalog_revision)
    return _load_team_row(scope,row,catalog)


@dataclass(frozen=True, slots=True)
class TrustedStepRequirements:
    """Owner/trusted-coordinator input; an LLM cannot weaken these fields."""

    task_id: str
    plan_id: str
    step_id: str
    skills: tuple[str, ...]
    actions: tuple[ProtectedAction, ...]

    def __post_init__(self) -> None:
        _id(self.task_id,_TASK_ID)
        _id(self.plan_id,_PLAN_ID)
        _id(self.step_id,_ROLE)
        if (type(self.skills) is not tuple or not 1 <= len(self.skills) <= 32
                or len(set(self.skills)) != len(self.skills)
                or any(type(s) is not str or not _ROLE.fullmatch(s) for s in self.skills)
                or type(self.actions) is not tuple or len(self.actions) > 5
                or len(set(self.actions)) != len(self.actions)
                or any(not isinstance(a,ProtectedAction) for a in self.actions)):
            raise InvalidSpecialist("invalid trusted step requirements")


@dataclass(frozen=True, slots=True)
class SpecialistCandidate:
    agent_id: str
    provider_id: str
    fallback_reason: str | None = None


def parse_candidate(raw: bytes) -> SpecialistCandidate:
    obj = _fields(_read(raw), {"agent_id","provider_id","fallback_reason"})
    reason = obj["fallback_reason"]
    if reason is not None:
        reason = _id(reason,_ROLE)
    return SpecialistCandidate(_id(obj["agent_id"],_PROJECT_ID),
                               _id(obj["provider_id"],_PROVIDER),reason)


@dataclass(frozen=True, slots=True)
class SpecialistPin:
    task_id: str
    plan_id: str
    plan_revision: int
    plan_digest: str
    step_id: str
    team_id: str
    team_revision: int
    team_digest: str
    catalog_revision: int
    catalog_digest: str
    agent_id: str
    role_id: str
    provider_id: str
    provider_reason: str | None
    allowed_actions: tuple[ProtectedAction, ...]


def assign_specialist(inbox: TaskInbox, scope: OwnerScope,
                      requirements: TrustedStepRequirements,
                      team_id: str, candidate: SpecialistCandidate,
                      *, grants: tuple[ActionGrant, ...]) -> SpecialistPin:
    """Independently validate GPT's choice, then atomic task claim + pin.

    Never accepts actions or skills from the model candidate. Requires an
    already admitted task and approved plan, catalog and team. No worker runs.
    """
    if (not isinstance(inbox,TaskInbox) or inbox._store is None
            or not isinstance(scope,OwnerScope)
            or not isinstance(requirements,TrustedStepRequirements)
            or not isinstance(candidate,SpecialistCandidate)
            or type(grants) is not tuple
            or any(not isinstance(g,ActionGrant) for g in grants)):
        raise InvalidSpecialist("invalid trusted specialist matching context")
    _id(team_id,_TEAM_ID)
    _id(candidate.agent_id,_PROJECT_ID)
    _id(candidate.provider_id,_PROVIDER)
    if candidate.fallback_reason is not None:
        _id(candidate.fallback_reason,_ROLE)
    plan = load_approved_plan(inbox,scope,requirements.plan_id)
    team = load_team(inbox,scope,team_id)
    if plan is None or team is None:
        with inbox._lock:
            record = inbox._scoped_record(requirements.task_id,scope)
            inbox._store.reject_specialist(record.task_id,scope.owner_id,
                                           record.project_id,candidate.agent_id,
                                           "unapproved_plan_or_team")
        raise SpecialistDenied("specialist_assignment_denied")
    catalog = load_catalog(inbox,scope,team.proposal.spec.catalog_revision)
    with inbox._lock:
        record = inbox._scoped_record(requirements.task_id,scope)
        def deny(reason: str) -> None:
            inbox._store.reject_specialist(record.task_id,scope.owner_id,
                                            record.project_id,candidate.agent_id,reason)
            raise SpecialistDenied("specialist_assignment_denied")

        p,t,c = plan.proposal,team.proposal,catalog.proposal
        latest_plan = inbox._store.latest_approved_plan(p.plan_id)
        latest_team = inbox._store.specialist_team(t.team_id)
        catalog_row = inbox._store.specialist_catalog(scope.owner_id,c.revision)
        if (record.project_id != t.project_id or record.project_id != p.source.project_id
                or latest_plan is None or latest_plan[1] != p.revision
                or latest_plan[6] != p.sha256 or latest_team is None
                or latest_team[1] != t.revision or latest_team[6] != t.sha256
                or catalog_row is None or catalog_row[4] != c.sha256):
            deny("stale_or_cross_scope")
        if not any(step.step_id == requirements.step_id for step in p.spec.steps):
            deny("step_missing")
        member = next((m for m in t.spec.members
                       if m.agent_id == candidate.agent_id),None)
        if member is None:
            deny("agent_missing")
        roles = {role.role_id:role for role in (*c.spec.roles,*t.spec.local_roles)}
        role = roles.get(member.role_id)
        if role is None or not set(requirements.skills).issubset(role.skills):
            deny("skills_missing")
        if not set(requirements.actions).issubset(member.actions):
            deny("role_policy_denied")
        if candidate.provider_id == role.primary_provider:
            if candidate.fallback_reason is not None:
                deny("unnecessary_provider_override")
        elif (candidate.provider_id not in role.fallback_providers
              or candidate.fallback_reason is None):
            deny("provider_policy_denied")
        if inbox._store.emergency_held(scope.owner_id,record.project_id):
            deny("emergency_hold")
        now = time.time()
        for action in requirements.actions:
            if (not any(g.task_id == record.task_id
                        and g.worker_id == candidate.agent_id
                        and g.action is action
                        and (g.expires_at is None or g.expires_at > now)
                        for g in grants)
                    or inbox._store.is_permission_revoked(record.task_id,
                                                           candidate.agent_id,action.value)):
                deny("grant_missing_or_revoked")
        actions_json = _json(sorted(a.value for a in member.actions))
        row = (record.task_id,scope.owner_id,record.project_id,
               p.plan_id,p.revision,p.sha256,requirements.step_id,
               t.team_id,t.revision,t.sha256,c.revision,c.sha256,
               member.agent_id,member.role_id,candidate.provider_id,
               candidate.fallback_reason,actions_json)
        # The same SQLite transaction stores task claim, state, version pin
        # and both assignment audits. Workspace lease is acquired before it.
        inbox._claim_task_locked(record.task_id,member.agent_id,
                                 member.workspace_name,scope,specialist_pin=row)
    return SpecialistPin(record.task_id,p.plan_id,p.revision,p.sha256,
                         requirements.step_id,t.team_id,t.revision,t.sha256,
                         c.revision,c.sha256,member.agent_id,member.role_id,
                         candidate.provider_id,candidate.fallback_reason,
                         member.actions)


def load_specialist_pin(inbox: TaskInbox, scope: OwnerScope,
                        task_id: str) -> SpecialistPin | None:
    if not isinstance(inbox,TaskInbox) or inbox._store is None or not isinstance(scope,OwnerScope):
        raise InvalidSpecialist("trusted specialist store and scope required")
    with inbox._lock:
        record = inbox._scoped_record(task_id,scope)
        row = inbox._store.specialist_pin(task_id)
    if row is None:
        return None
    (tid,owner,project,plan_id,plan_rev,plan_digest,step_id,team_id,
     team_rev,team_digest,catalog_rev,catalog_digest,agent,role,provider,
     provider_reason,actions_json) = row
    if (owner != scope.owner_id or project != record.project_id
            or type(plan_digest) is not str or not _DIGEST.fullmatch(plan_digest)):
        raise StorageUnavailable("specialist pin scope or digest corrupted")
    team = load_team(inbox,scope,team_id,team_rev)
    if team is None or team.proposal.sha256 != team_digest:
        raise StorageUnavailable("specialist team pin corrupted")
    catalog = load_catalog(inbox,scope,catalog_rev)
    if catalog is None or catalog.proposal.sha256 != catalog_digest:
        raise StorageUnavailable("specialist catalog pin corrupted")
    with inbox._lock:
        approved_plan = inbox._store._db().execute("""SELECT plan_sha256,canonical_json FROM
            approved_task_plans WHERE plan_id=? AND revision=? AND owner_id=?
            AND project_id=?""",(plan_id,plan_rev,owner,project)).fetchone()
        claim = inbox._assignments.get(tid)
    if (approved_plan is None or approved_plan[0] != plan_digest
            or type(approved_plan[1]) is not str
            or sha256(approved_plan[1].encode("utf-8")).hexdigest() != plan_digest
            or claim is None or claim.worker_id != agent):
        raise StorageUnavailable("specialist plan or claim pin corrupted")
    try:
        plan_body = json.loads(approved_plan[1])
        if step_id not in {s["step_id"] for s in plan_body["draft"]["steps"]}:
            raise ValueError("missing step in saved plan")
    except (ValueError,KeyError,TypeError,UnicodeError) as exc:
        raise StorageUnavailable("specialist plan step pin corrupted") from exc
    member = next((m for m in team.proposal.spec.members if m.agent_id == agent),None)
    profile = next((r for r in (*catalog.proposal.spec.roles,
                               *team.proposal.spec.local_roles)
                    if r.role_id == role),None)
    if (member is None or member.role_id != role
            or claim.workspace_name != member.workspace_name
            or profile is None
            or provider not in (profile.primary_provider,*profile.fallback_providers)
            or (provider == profile.primary_provider and provider_reason is not None)
            or (provider != profile.primary_provider and
                (type(provider_reason) is not str or not _ROLE.fullmatch(provider_reason)))
            or actions_json != _json(sorted(a.value for a in member.actions))):
        raise StorageUnavailable("specialist member pin corrupted")
    return SpecialistPin(tid,plan_id,plan_rev,plan_digest,step_id,team_id,
                         team_rev,team_digest,catalog_rev,catalog_digest,
                         agent,role,provider,provider_reason,member.actions)


def run_pinned_action(inbox: TaskInbox, gate: TaskPermissionGate,
                      scope: OwnerScope, task_id: str, *, actor_id: str,
                      action: ProtectedAction, operation: Callable[[], _T]) -> _T:
    """Optional 7.3 runtime guard for future specialist dispatch integrations.

    The underlying permission gate still checks the *current* revocation,
    emergency hold, task state, TTL and exact assigned worker per action.
    """
    if not isinstance(gate,TaskPermissionGate) or gate.inbox is not inbox:
        raise InvalidSpecialist("wrong task permission gate")
    pin = load_specialist_pin(inbox,scope,task_id)
    if (pin is None or actor_id != pin.agent_id or not isinstance(action,ProtectedAction)
            or action not in pin.allowed_actions):
        raise PermissionDenied("permission_denied")
    return gate.run(task_id,scope,actor_id=actor_id,action=action,operation=operation)


def specialist_audit(inbox: TaskInbox, scope: OwnerScope,
                     task_id: str) -> tuple[tuple, ...]:
    if not isinstance(inbox,TaskInbox) or inbox._store is None or not isinstance(scope,OwnerScope):
        raise InvalidSpecialist("trusted specialist store and scope required")
    with inbox._lock:
        inbox._scoped_record(task_id,scope)
        return tuple(inbox._store.specialist_audit(task_id))
