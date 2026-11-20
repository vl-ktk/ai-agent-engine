"""7.9: durable owner-appointed global direction leads (offline contract).

An appointment coordinates a specialization across projects; it does not
create a project membership, task claim, ActionGrant, secret or source access.
The trusted caller authenticates the owner before constructing OwnerDecision.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from .main_orchestrator import OwnerDecision
from .main_specialists import (_PROJECT_ID, _PROPOSAL_ID, _ROLE, _id, _json,
                               load_catalog)
from .task_api import OwnerScope, TaskInbox
from .task_assignment import validate_worker
from .task_storage import StorageUnavailable


class GlobalLeadDenied(PermissionError):
    """Invalid/stale owner decision; public errors contain no proposal data."""


@dataclass(frozen=True, slots=True)
class GlobalLeadProposal:
    owner_id: str
    direction_id: str
    revision: int
    action: str
    agent_id: str | None
    role_id: str | None
    catalog_revision: int
    catalog_sha256: str | None
    previous_sha256: str | None
    proposal_id: str
    canonical_json: str
    sha256: str


@dataclass(frozen=True, slots=True)
class GlobalLead:
    direction_id: str
    agent_id: str
    role_id: str
    revision: int
    catalog_revision: int


def _context(inbox: TaskInbox, scope: OwnerScope, direction_id: str) -> None:
    if (not isinstance(inbox, TaskInbox) or inbox._store is None
            or not isinstance(scope, OwnerScope)):
        raise GlobalLeadDenied("trusted owner and persistent inbox required")
    _id(direction_id, _ROLE)


def _proposal(owner: str, direction: str, revision: int, action: str,
              agent: str | None, role: str | None, catalog_revision: int,
              catalog_digest: str | None, previous: str | None,
              proposal_id: str) -> GlobalLeadProposal:
    _id(owner, _PROJECT_ID)
    _id(direction, _ROLE)
    _id(proposal_id, _PROPOSAL_ID)
    if (type(revision) is not int or not 1 <= revision <= 1_000_000
            or action not in ("appoint", "remove")
            or type(action) is not str):
        raise GlobalLeadDenied("invalid global lead revision or action")
    if previous is not None and (type(previous) is not str or len(previous) != 64
                                 or any(ch not in "0123456789abcdef" for ch in previous)):
        raise GlobalLeadDenied("invalid previous decision")
    if action == "appoint":
        _id(agent, _PROJECT_ID)
        validate_worker(agent)
        _id(role, _ROLE)
        if (type(catalog_revision) is not int or not 1 <= catalog_revision <= 1_000_000
                or type(catalog_digest) is not str or len(catalog_digest) != 64
                or any(ch not in "0123456789abcdef" for ch in catalog_digest)):
            raise GlobalLeadDenied("invalid approved catalog reference")
    elif agent is not None or role is not None or catalog_revision != 0 or catalog_digest is not None:
        raise GlobalLeadDenied("removal cannot assign a role")
    body = {"owner_id":owner, "direction_id":direction,
            "revision":revision, "action":action, "agent_id":agent,
            "role_id":role, "catalog_revision":catalog_revision,
            "catalog_sha256":catalog_digest, "previous_sha256":previous,
            "proposal_id":proposal_id}
    canonical = _json(body)
    return GlobalLeadProposal(owner,direction,revision,action,agent,role,
                              catalog_revision,catalog_digest,previous,
                              proposal_id,canonical,
                              sha256(canonical.encode("utf-8")).hexdigest())


def _verified_history(inbox: TaskInbox, scope: OwnerScope,
                      direction_id: str | None) -> tuple[GlobalLeadProposal, ...]:
    """Validate all versions, including the removal ledger and CAS chain."""
    rows = inbox._store.global_lead_rows(scope.owner_id,direction_id)
    verified: list[GlobalLeadProposal] = []
    last: dict[str, GlobalLeadProposal] = {}
    for row in rows:
        (owner,direction,revision,action,agent,role,cat_rev,cat_digest,
         previous,proposal_id,channel,digest,canonical) = row
        earlier = last.get(direction)
        try:
            item = _proposal(owner,direction,revision,action,agent,role,
                             cat_rev,cat_digest,previous,proposal_id)
        except (ValueError, TypeError, GlobalLeadDenied) as exc:
            raise StorageUnavailable("invalid global lead ledger") from exc
        if (owner != scope.owner_id or revision != (earlier.revision+1 if earlier else 1)
                or previous != (earlier.sha256 if earlier else None)
                or channel not in ("message", "button")
                or canonical != item.canonical_json or digest != item.sha256):
            raise StorageUnavailable("global lead ledger integrity failure")
        last[direction] = item
        verified.append(item)
    return tuple(verified)


def draft_global_lead(inbox: TaskInbox, scope: OwnerScope, *,
                      direction_id: str, agent_id: str,
                      role_id: str) -> GlobalLeadProposal:
    """Prepare an appointment. No mutation or permission results from a draft."""
    _context(inbox,scope,direction_id)
    _id(agent_id,_PROJECT_ID)
    validate_worker(agent_id)
    _id(role_id,_ROLE)
    with inbox._lock:
        history = _verified_history(inbox,scope,direction_id)
        catalog = load_catalog(inbox,scope)
        if (catalog is None or not any(r.role_id == role_id
                                       for r in catalog.proposal.spec.roles)):
            raise GlobalLeadDenied("role absent from owner-approved catalog")
        previous = history[-1] if history else None
        return _proposal(scope.owner_id,direction_id,
                         previous.revision+1 if previous else 1,"appoint",
                         agent_id,role_id,catalog.proposal.revision,
                         catalog.proposal.sha256,previous.sha256 if previous else None,
                         "proposal_"+uuid4().hex)


def draft_global_lead_removal(inbox: TaskInbox, scope: OwnerScope, *,
                              direction_id: str) -> GlobalLeadProposal:
    _context(inbox,scope,direction_id)
    with inbox._lock:
        history = _verified_history(inbox,scope,direction_id)
        if not history or history[-1].action != "appoint":
            raise GlobalLeadDenied("no appointed global lead")
        previous = history[-1]
        return _proposal(scope.owner_id,direction_id,previous.revision+1,
                         "remove",None,None,0,None,previous.sha256,
                         "proposal_"+uuid4().hex)


def approve_global_lead(inbox: TaskInbox, scope: OwnerScope,
                        proposal: GlobalLeadProposal,
                        decision: OwnerDecision) -> GlobalLead | None:
    """Exact owner-authenticated appointment/removal, atomic and replay-safe."""
    if not isinstance(proposal,GlobalLeadProposal):
        raise GlobalLeadDenied("invalid global lead proposal")
    _context(inbox,scope,proposal.direction_id)
    if (not isinstance(decision,OwnerDecision)
            or decision.owner_id != scope.owner_id
            or proposal.owner_id != scope.owner_id
            or decision.proposal_id != proposal.proposal_id
            or decision.proposal_sha256 != proposal.sha256
            or type(decision.channel) is not str
            or decision.channel not in ("message","button")):
        raise GlobalLeadDenied("owner approval does not match global lead")
    check = _proposal(proposal.owner_id,proposal.direction_id,
                      proposal.revision,proposal.action,proposal.agent_id,
                      proposal.role_id,proposal.catalog_revision,
                      proposal.catalog_sha256,proposal.previous_sha256,
                      proposal.proposal_id)
    if check != proposal:
        raise GlobalLeadDenied("global lead proposal changed after review")
    with inbox._lock:
        history = _verified_history(inbox,scope,proposal.direction_id)
        if history and history[-1] == proposal:
            # Repeat of the same confirmed decision is idempotent.
            pass
        else:
            previous = history[-1] if history else None
            if (proposal.revision != (previous.revision+1 if previous else 1)
                    or proposal.previous_sha256 != (previous.sha256 if previous else None)
                    or (proposal.action == "remove" and
                        (previous is None or previous.action != "appoint"))):
                raise GlobalLeadDenied("stale global lead proposal")
            if proposal.action == "appoint":
                catalog = load_catalog(inbox,scope)
                if (catalog is None
                        or catalog.proposal.revision != proposal.catalog_revision
                        or catalog.proposal.sha256 != proposal.catalog_sha256
                        or not any(r.role_id == proposal.role_id
                                   for r in catalog.proposal.spec.roles)):
                    raise GlobalLeadDenied("approved catalog changed")
            row = (proposal.owner_id,proposal.direction_id,proposal.revision,
                   proposal.action,proposal.agent_id,proposal.role_id,
                   proposal.catalog_revision,proposal.catalog_sha256,
                   proposal.previous_sha256,proposal.proposal_id,
                   decision.channel,proposal.sha256,proposal.canonical_json)
            inbox._store.save_global_lead(row,proposal.previous_sha256,
                                           catalog_digest=proposal.catalog_sha256)
    if proposal.action == "remove":
        return None
    return GlobalLead(proposal.direction_id,proposal.agent_id,proposal.role_id,
                      proposal.revision,proposal.catalog_revision)


def load_global_lead(inbox: TaskInbox, scope: OwnerScope, *,
                     direction_id: str) -> GlobalLead | None:
    """No execution authorization: reports only currently valid owner choice."""
    _context(inbox,scope,direction_id)
    with inbox._lock:
        history = _verified_history(inbox,scope,direction_id)
        if not history or history[-1].action == "remove":
            return None
        latest = history[-1]
        catalog = load_catalog(inbox,scope)
        if (catalog is None or catalog.proposal.revision != latest.catalog_revision
                or catalog.proposal.sha256 != latest.catalog_sha256
                or not any(r.role_id == latest.role_id
                           for r in catalog.proposal.spec.roles)):
            raise GlobalLeadDenied("global lead must be reapproved for current catalog")
        return GlobalLead(direction_id,latest.agent_id,latest.role_id,
                          latest.revision,latest.catalog_revision)


def global_lead_audit(inbox: TaskInbox, scope: OwnerScope, *,
                      direction_id: str) -> tuple[tuple[int,str,str], ...]:
    """Owner-scoped revision/action/digest, without provider or secret data."""
    _context(inbox,scope,direction_id)
    with inbox._lock:
        history = _verified_history(inbox,scope,direction_id)
    return tuple((p.revision,p.action,p.sha256) for p in history)
