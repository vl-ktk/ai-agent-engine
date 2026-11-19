"""Stage 7.4: offline owner-only reassignment and immutable attempt ledger.

This provider-neutral contract does not start a subprocess or invoke a model.
Only an authenticated owner adapter may construct OwnerDecision, and only a
trusted supervisor/QA caller may supply external-effect and artifact evidence.
The old TaskInbox claim is deliberately NOT overwritten: its process and
permission interfaces are fenced as soon as a new attempt is approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
import sqlite3
import time
from uuid import uuid4

from .main_orchestrator import OwnerDecision
from .main_specialists import (
    SpecialistCandidate, TrustedStepRequirements, load_catalog,
    load_specialist_pin, load_team,
)
from .task_api import OwnerScope, ProjectForbidden, TaskInbox, _TASK_ID
from .task_assignment import validate_worker
from .task_permissions import ActionGrant, ProtectedAction
from .task_process_identity import group_has_live_members
from .task_state import TaskState
from .task_storage import StorageUnavailable


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT = re.compile(r"attempt_[0-9a-f]{32}\Z")
_STATES = {"assigned", "running", "failed", "cancelled", "timed_out"}
_RETRYABLE = {"failed", "cancelled", "timed_out"}


class TransferDenied(PermissionError):
    """Sanitized refusal; no prompts, secrets or foreign task contents."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class CheckpointFile:
    """Trusted QA result or unverified draft, never model-declared verified."""

    path: str
    body: bytes
    evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path) if type(self.path) is str else None
        if (path is None or not self.path or len(self.path) > 240
                or self.path.startswith("/") or "\\" in self.path
                or path.as_posix() != self.path or not path.parts or ".." in path.parts
                or any(part in ("", ".") for part in path.parts)
                or type(self.body) is not bytes or len(self.body) > 2_097_152
                or (self.evidence_sha256 is not None and
                    (type(self.evidence_sha256) is not str or
                     not _DIGEST.fullmatch(self.evidence_sha256)))):
            raise ValueError("invalid checkpoint artifact")

    @property
    def sha256(self) -> str:
        return sha256(self.body).hexdigest()

    @property
    def status(self) -> str:
        return "verified" if self.evidence_sha256 is not None else "unverified"


@dataclass(frozen=True, slots=True)
class EffectClearance:
    """Trusted supervisor receipt for previously executed external effects."""

    task_id: str
    source_attempt_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if (type(self.task_id) is not str or not _TASK_ID.fullmatch(self.task_id)
                or type(self.source_attempt_id) is not str
                or self.source_attempt_id != "legacy" and
                   not _ATTEMPT.fullmatch(self.source_attempt_id)
                or type(self.evidence_sha256) is not str
                or not _DIGEST.fullmatch(self.evidence_sha256)):
            raise ValueError("invalid external-effect receipt")


@dataclass(frozen=True, slots=True)
class TransferProposal:
    proposal_id: str
    owner_id: str
    project_id: str
    task_id: str
    source_attempt_id: str
    source_revision: int
    source_state: str
    source_agent: str
    source_provider: str
    task_priority: int
    target_agent: str
    target_provider: str
    workspace_name: str
    team_id: str
    team_revision: int
    team_sha256: str
    plan_id: str
    plan_revision: int
    plan_sha256: str
    catalog_revision: int
    catalog_sha256: str
    role_actions: tuple[ProtectedAction, ...]
    required_actions: tuple[ProtectedAction, ...]
    checkpoint: tuple[CheckpointFile, ...]
    reason: str
    created_at: float
    expires_at: float
    sha256: str

    def body(self) -> dict[str, object]:
        return {"version": 1, "proposal_id": self.proposal_id,
                "owner_id": self.owner_id, "project_id": self.project_id,
                "task_id": self.task_id, "source_attempt_id": self.source_attempt_id,
                "source_revision": self.source_revision,
                "source_state": self.source_state,
                "source_agent": self.source_agent,
                "source_provider": self.source_provider,
                "task_priority": self.task_priority,
                "target_agent": self.target_agent,
                "target_provider": self.target_provider,
                "workspace_name": self.workspace_name,
                "team_id": self.team_id, "team_revision": self.team_revision,
                "team_sha256": self.team_sha256,
                "plan_id": self.plan_id,
                "plan_revision": self.plan_revision,
                "plan_sha256": self.plan_sha256,
                "catalog_revision": self.catalog_revision,
                "catalog_sha256": self.catalog_sha256,
                "role_actions": sorted(a.value for a in self.role_actions),
                "required_actions": sorted(a.value for a in self.required_actions),
                "checkpoint": [{"path": f.path, "sha256": f.sha256,
                                "status": f.status,
                                "evidence_sha256": f.evidence_sha256}
                               for f in self.checkpoint],
                "reason":self.reason,
                "created_at": self.created_at, "expires_at": self.expires_at}


@dataclass(frozen=True, slots=True)
class TransferAttempt:
    task_id: str
    attempt_id: str
    ordinal: int
    agent_id: str
    provider_id: str
    state: str


def owner_transfer_card(proposal: TransferProposal,
                        *, expanded: bool = False) -> dict[str, object]:
    """Exact-version owner card: no task prompts, checkpoint bytes or secrets."""
    if not isinstance(proposal,TransferProposal) or type(expanded) is not bool:
        raise TransferDenied("invalid_owner_card")
    verified = sum(f.status == "verified" for f in proposal.checkpoint)
    card: dict[str,object] = {
        "task_id":proposal.task_id,
        "source_attempt_id":proposal.source_attempt_id,
        "source_state":proposal.source_state,
        "source_agent":proposal.source_agent,
        "source_provider":proposal.source_provider,
        "reason":proposal.reason,
        "target_agent":proposal.target_agent,
        "target_provider":proposal.target_provider,
        "verified_files":verified,
        "unverified_files":len(proposal.checkpoint)-verified,
        "requested_actions":sorted(a.value for a in proposal.required_actions),
        "risks":(["unverified_checkpoint"] if verified < len(proposal.checkpoint)
                  else []) + (["external_effect_clearance_needed"]
                              if proposal.source_state in _RETRYABLE else []),
        "approval_sha256":proposal.sha256,
        "expires_at":proposal.expires_at,
    }
    if expanded:
        card["plan_id"] = proposal.plan_id
        card["plan_revision"] = proposal.plan_revision
        card["team_id"] = proposal.team_id
        card["team_revision"] = proposal.team_revision
        card["checkpoint_manifest"] = proposal.body()["checkpoint"]
    return card


@dataclass(frozen=True, slots=True)
class WaitTier:
    """One owner-policy priority band, without implicit numeric defaults."""

    minimum_priority: int
    maximum_seconds: int
    maximum_checks: int

    def __post_init__(self) -> None:
        if (type(self.minimum_priority) is not int
                or not 0 <= self.minimum_priority <= 100
                or type(self.maximum_seconds) is not int
                or not 1 <= self.maximum_seconds <= 604800
                or type(self.maximum_checks) is not int
                or not 1 <= self.maximum_checks <= 10000):
            raise ValueError("invalid wait tier")


@dataclass(frozen=True, slots=True)
class PriorityWaitPolicy:
    """Trusted, externally owner-approved future 7.11/7.12 policy input.

    It is informational in this offline module: it cannot grant execution or
    automatically select a substitute agent.
    """

    owner_id: str
    project_id: str
    revision: int
    tiers: tuple[WaitTier, ...]

    def __post_init__(self) -> None:
        if (type(self.revision) is not int or self.revision < 1
                or type(self.tiers) is not tuple or not 1 <= len(self.tiers) <= 10
                or any(not isinstance(t, WaitTier) for t in self.tiers)
                or self.tiers[0].minimum_priority != 0
                or any(a.minimum_priority >= b.minimum_priority
                       for a,b in zip(self.tiers,self.tiers[1:]))):
            raise ValueError("invalid priority wait policy")


@dataclass(frozen=True, slots=True)
class WaitAdvice:
    attempt_id: str
    action: str  # wait / suggest_alternative / hold
    checks: int
    elapsed_seconds: float


def _source_locked(inbox: TaskInbox, task_id: str) -> tuple[str, int, str]:
    row = inbox._store._db().execute("""SELECT c.attempt_id,c.revision,a.state
        FROM task_transfer_current c JOIN task_transfer_attempts a
        ON a.attempt_id=c.attempt_id WHERE c.task_id=?""", (task_id,)).fetchone()
    if row is None:
        return ("legacy", 0, inbox._states[task_id].value)
    return (row[0], row[1], row[2])


def _check_stopped_locked(inbox: TaskInbox, task_id: str,
                          source_id: str, state: str,
                          clearance: EffectClearance | None) -> None:
    if state not in _STATES and state != "waiting":
        raise TransferDenied("transfer_source_unavailable")
    if source_id != "legacy" and state == "running":
        # v14 does not launch managed workers: a later provider adapter must
        # supply verified per-attempt process identities before enabling this.
        raise TransferDenied("attempt_process_not_reconciled")
    if source_id == "legacy":
        row = inbox._store.process_record(task_id)
        if row is None and state == "running":
            raise TransferDenied("process_untracked")
        if row is not None:
            if row[-1] != "stopped" or group_has_live_members(row[2]):
                raise TransferDenied("process_not_stopped")
    if state in _RETRYABLE and (
            not isinstance(clearance, EffectClearance)
            or clearance.task_id != task_id
            or clearance.source_attempt_id != source_id):
        raise TransferDenied("external_effect_not_reconciled")


def _grants_valid(inbox: TaskInbox, proposal: TransferProposal,
                  grants: tuple[ActionGrant, ...], now: float) -> bool:
    if type(grants) is not tuple or any(not isinstance(g, ActionGrant) for g in grants):
        return False
    return all(any(g.task_id == proposal.task_id and
                   g.worker_id == proposal.target_agent and g.action is action and
                   (g.expires_at is None or g.expires_at > now) for g in grants)
               and not inbox._store.is_permission_revoked(
                   proposal.task_id, proposal.target_agent, action.value)
               for action in proposal.required_actions)


def draft_transfer(inbox: TaskInbox, scope: OwnerScope,
                   requirements: TrustedStepRequirements,
                   candidate: SpecialistCandidate,
                   checkpoint: tuple[CheckpointFile, ...],
                   *, ttl_seconds: int,
                   reason: str = "manual_transfer") -> TransferProposal:
    """Propose ONLY; role/skills and versions come from approved 7.3 data."""
    if (not isinstance(inbox, TaskInbox) or inbox._store is None
            or not isinstance(scope, OwnerScope)
            or not isinstance(requirements, TrustedStepRequirements)
            or not isinstance(candidate, SpecialistCandidate)
            or type(checkpoint) is not tuple or len(checkpoint) > 16
            or any(not isinstance(f, CheckpointFile) for f in checkpoint)
            or len({f.path for f in checkpoint}) != len(checkpoint)
            or sum(len(f.body) for f in checkpoint) > 8_388_608
            or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400
            or type(reason) is not str or not 1 <= len(reason) <= 120
            or not re.fullmatch(r"[a-z][a-z0-9_]*",reason)):
        raise TransferDenied("invalid_transfer_request")
    pin = load_specialist_pin(inbox, scope, requirements.task_id)
    if (pin is None or pin.plan_id != requirements.plan_id
            or pin.step_id != requirements.step_id):
        raise TransferDenied("unapproved_source")
    team = load_team(inbox, scope, pin.team_id)
    if team is None:
        raise TransferDenied("team_unavailable")
    catalog = load_catalog(inbox, scope, team.proposal.spec.catalog_revision)
    if catalog is None:
        raise TransferDenied("catalog_unavailable")
    member = next((m for m in team.proposal.spec.members
                   if m.agent_id == candidate.agent_id), None)
    roles = {r.role_id: r for r in (*catalog.proposal.spec.roles,
                                    *team.proposal.spec.local_roles)}
    role = roles.get(member.role_id) if member else None
    if (member is None or role is None
            or not set(requirements.skills).issubset(role.skills)
            or not set(requirements.actions).issubset(member.actions)
            or candidate.provider_id not in (role.primary_provider,
                                              *role.fallback_providers)
            or (candidate.provider_id == role.primary_provider)
               != (candidate.fallback_reason is None)):
        raise TransferDenied("candidate_not_permitted")
    with inbox._lock:
        record = inbox._scoped_record(requirements.task_id, scope)
        source_id, rev, state = _source_locked(inbox, record.task_id)
        if source_id == "legacy":
            source_agent,source_provider = pin.agent_id,pin.provider_id
        else:
            source_info = inbox._store._db().execute(
                "SELECT agent_id,provider_id FROM task_transfer_attempts "
                "WHERE attempt_id=?",(source_id,)).fetchone()
            if source_info is None:
                raise TransferDenied("source_attempt_missing")
            source_agent,source_provider = source_info
        priority = inbox._priorities[record.task_id]
        if (record.project_id != team.proposal.spec.project_id
                or state not in _STATES | {"waiting"}
                or inbox._store.emergency_held(scope.owner_id, record.project_id)):
            raise TransferDenied("source_or_project_unavailable")
    now = time.time()
    draft = TransferProposal("proposal_" + uuid4().hex, scope.owner_id,
        record.project_id, record.task_id, source_id, rev, state,
        source_agent,source_provider,priority,
        member.agent_id, candidate.provider_id, member.workspace_name,
        pin.team_id, team.proposal.revision, team.proposal.sha256,
        pin.plan_id, pin.plan_revision, pin.plan_digest,
        pin.catalog_revision, pin.catalog_digest,
        member.actions, requirements.actions, checkpoint,reason,now,
        now + ttl_seconds, "")
    from dataclasses import replace
    return replace(draft, sha256=sha256(_json(draft.body()).encode()).hexdigest())


def approve_transfer(inbox: TaskInbox, scope: OwnerScope,
                     proposal: TransferProposal, decision: OwnerDecision,
                     *, grants: tuple[ActionGrant, ...],
                     clearance: EffectClearance | None = None) -> TransferAttempt:
    """Atomic exact-owner approval, old-attempt retirement and checkpoint save."""
    if (not isinstance(inbox, TaskInbox) or inbox._store is None
            or not isinstance(scope, OwnerScope)
            or not isinstance(proposal, TransferProposal)
            or not isinstance(decision, OwnerDecision)
            or decision.channel not in ("button", "message")
            or decision.owner_id != scope.owner_id
            or proposal.owner_id != scope.owner_id
            or decision.proposal_id != proposal.proposal_id
            or decision.proposal_sha256 != proposal.sha256
            or proposal.sha256 != sha256(_json(proposal.body()).encode()).hexdigest()):
        raise TransferDenied("owner_approval_mismatch")
    now = time.time()
    if now >= proposal.expires_at or now < proposal.created_at - 5:
        raise TransferDenied("proposal_expired")
    with inbox._transfer_barrier(proposal.task_id,scope), inbox._lock:
        record = inbox._scoped_record(proposal.task_id, scope)
        if record.project_id != proposal.project_id:
            raise TransferDenied("owner_scope_mismatch")
        db = inbox._store._db()
        source_id, rev, state = _source_locked(inbox, proposal.task_id)
        if (source_id != proposal.source_attempt_id
                or rev != proposal.source_revision
                or state != proposal.source_state
                or inbox._priorities[record.task_id] != proposal.task_priority):
            raise TransferDenied("proposal_stale")
        team = inbox._store.specialist_team(proposal.team_id)
        pin = inbox._store.specialist_pin(proposal.task_id)
        if (team is None or team[1] != proposal.team_revision
                or team[3:5] != (scope.owner_id, record.project_id)
                or team[6] != proposal.team_sha256
                or pin is None
                or pin[3:6] != (proposal.plan_id,proposal.plan_revision,
                                proposal.plan_sha256)
                or pin[10:12] != (proposal.catalog_revision,
                                  proposal.catalog_sha256)
                or inbox._store.emergency_held(scope.owner_id, record.project_id)
                or not _grants_valid(inbox, proposal, grants, now)):
            raise TransferDenied("policy_or_grants_stale")
        _check_stopped_locked(inbox, proposal.task_id, source_id, state, clearance)
        # No caller may authorize a second writer by approving the same
        # source revision twice. The unique current-row CAS is transactionally
        # sequenced under the inbox lock and SQLite exclusive writer lock.
        attempt_id = "attempt_" + uuid4().hex
        ordinal = rev + 1
        manifest = sha256(_json(proposal.body()["checkpoint"]).encode()).hexdigest()
        try:
            db.execute("BEGIN IMMEDIATE")
            with db:
                db.execute("""INSERT INTO task_transfer_proposals
                    (proposal_id,task_id,owner_id,project_id,proposal_sha256,
                     body_json,expires_at,status,approved_channel)
                    VALUES (?,?,?,?,?, ?,?,'approved',?)""",
                    (proposal.proposal_id,proposal.task_id,scope.owner_id,
                     record.project_id,proposal.sha256,_json(proposal.body()),
                     proposal.expires_at,decision.channel))
                if rev == 0:
                    pin = inbox._store.specialist_pin(proposal.task_id)
                    if pin is None:
                        raise TransferDenied("source_pin_missing")
                    db.execute("""INSERT INTO task_transfer_attempts
                        (attempt_id,task_id,ordinal,agent_id,provider_id,state,
                         source_proposal_id,checkpoint_sha256,team_revision,
                         team_sha256,role_actions_json)
                        VALUES (?,?,0,?,?,'retired',NULL,?,?,?,?)""",
                        ("attempt_" + uuid4().hex,proposal.task_id,pin[12],pin[14],
                         "0"*64,pin[8],pin[9],pin[16]))
                else:
                    db.execute("""UPDATE task_transfer_attempts SET state='superseded'
                        WHERE task_id=? AND attempt_id=? AND state IN
                        ('waiting','failed','cancelled','timed_out')""",
                        (proposal.task_id,source_id))
                    if db.execute("SELECT changes()").fetchone()[0] != 1:
                        raise TransferDenied("source_state_changed")
                    db.execute("""INSERT INTO task_transfer_audit
                        (task_id,proposal_id,attempt_id,action,outcome,reason_code)
                        VALUES (?,?,?,'supersede','allowed','new_owner_approval')""",
                        (proposal.task_id,proposal.proposal_id,source_id))
                db.execute("""INSERT INTO task_transfer_attempts
                    (attempt_id,task_id,ordinal,agent_id,provider_id,state,
                     source_proposal_id,checkpoint_sha256,team_revision,
                     team_sha256,role_actions_json,wait_started_at)
                    VALUES (?,?,?,?,?,'waiting',?,?,?,?,?,?)""",
                    (attempt_id,proposal.task_id,ordinal,proposal.target_agent,
                     proposal.target_provider,proposal.proposal_id,manifest,
                     proposal.team_revision,proposal.team_sha256,
                     _json(sorted(a.value for a in proposal.role_actions)),now))
                for artifact in proposal.checkpoint:
                    db.execute("""INSERT INTO task_transfer_artifacts
                        (attempt_id,path,sha256,status,evidence_sha256,body)
                        VALUES (?,?,?,?,?,?)""",
                        (attempt_id,artifact.path,artifact.sha256,artifact.status,
                         artifact.evidence_sha256,artifact.body))
                db.execute("""INSERT INTO task_transfer_current(task_id,attempt_id,revision)
                    VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                    attempt_id=excluded.attempt_id,revision=excluded.revision""",
                    (proposal.task_id,attempt_id,ordinal))
                db.execute("""INSERT INTO task_transfer_audit
                    (task_id,proposal_id,attempt_id,action,outcome,reason_code)
                    VALUES (?,?,?,'approve','allowed','owner_approved')""",
                    (proposal.task_id,proposal.proposal_id,attempt_id))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist owner transfer") from exc
        return TransferAttempt(proposal.task_id,attempt_id,ordinal,
                               proposal.target_agent,proposal.target_provider,
                               "waiting")


def _check_checkpoint_manifest_locked(db: sqlite3.Connection, attempt_id: str,
                                      manifest: list[dict[str, object]]) -> None:
    records = db.execute("""SELECT path,sha256,status,evidence_sha256,body
        FROM task_transfer_artifacts WHERE attempt_id=?""",(attempt_id,)).fetchall()
    expected = {v["path"]:v for v in manifest}
    if len(records) != len(manifest) or len(expected) != len(manifest):
        raise StorageUnavailable("checkpoint manifest inconsistent")
    for path,digest,status,evidence,content in records:
        item = expected.get(path)
        if (item is None or item != {"path":path,"sha256":digest,
                                     "status":status,"evidence_sha256":evidence}
                or sha256(content).hexdigest() != digest):
            raise StorageUnavailable("checkpoint manifest inconsistent")


def _approval_context_locked(inbox: TaskInbox, scope: OwnerScope,
                             attempt_id: str, grants: tuple[ActionGrant, ...],
                             *, now: float) -> tuple:
    """Shared fail-closed revalidation for readiness and availability polling."""
    db = inbox._store._db()
    row = db.execute("""SELECT a.task_id,a.ordinal,a.agent_id,a.provider_id,
        a.state,a.source_proposal_id,p.body_json,p.expires_at,p.proposal_sha256,
        a.checkpoint_sha256,a.wait_started_at,a.wait_checks
        FROM task_transfer_attempts a JOIN task_transfer_proposals p
        ON p.proposal_id=a.source_proposal_id
        JOIN task_transfer_current c ON c.attempt_id=a.attempt_id
        WHERE a.attempt_id=?""",(attempt_id,)).fetchone()
    if row is None:
        raise TransferDenied("attempt_unavailable")
    record = inbox._scoped_record(row[0],scope)
    body = json.loads(row[6])
    if (sha256(row[6].encode()).hexdigest() != row[8]
            or body["proposal_id"] != row[5]
            or body["target_agent"] != row[2]
            or body["target_provider"] != row[3]
            or body["task_id"] != row[0]
            or body["task_priority"] != inbox._priorities[row[0]]
            or now >= row[7]
            or now < body["created_at"] - 5
            or body["owner_id"] != scope.owner_id
            or body["project_id"] != record.project_id
            or inbox._store.emergency_held(scope.owner_id,record.project_id)):
        raise TransferDenied("approval_expired_or_changed")
    pin = inbox._store.specialist_pin(row[0])
    team = inbox._store.specialist_team(body["team_id"])
    if (team is None or team[1] != body["team_revision"]
            or team[3:5] != (scope.owner_id, record.project_id)
            or team[6] != body["team_sha256"]
            or pin is None
            or pin[3:6] != (body["plan_id"],body["plan_revision"],
                            body["plan_sha256"])
            or pin[10:12] != (body["catalog_revision"],
                              body["catalog_sha256"])):
        raise TransferDenied("team_changed")
    proposed = TransferProposal(
        row[5],scope.owner_id,record.project_id,row[0],
        body["source_attempt_id"],body["source_revision"],
        body["source_state"],body["source_agent"],body["source_provider"],
        body["task_priority"],row[2],row[3],
        body["workspace_name"],
        body["team_id"],body["team_revision"],body["team_sha256"],
        body["plan_id"],body["plan_revision"],body["plan_sha256"],
        body["catalog_revision"],body["catalog_sha256"],
        tuple(ProtectedAction(a) for a in body["role_actions"]),
        tuple(ProtectedAction(a) for a in body["required_actions"]),
        (),body["reason"],body["created_at"],row[7],row[8])
    if not _grants_valid(inbox,proposed,grants,now):
        raise TransferDenied("permission_expired_or_revoked")
    if sha256(_json(body["checkpoint"]).encode()).hexdigest() != row[9]:
        raise StorageUnavailable("checkpoint manifest inconsistent")
    _check_checkpoint_manifest_locked(db,attempt_id,body["checkpoint"])
    # The original process remains stopped even while an approved candidate
    # is waiting. A runnable process or unknown process state holds activation.
    old = inbox._store.process_record(row[0])
    if old is not None:
        if old[-1] != "stopped" or group_has_live_members(old[2]):
            raise TransferDenied("legacy_process_live")
    elif inbox._states[row[0]] is TaskState.RUNNING:
        raise TransferDenied("legacy_process_untracked")
    return row


def activate_transfer(inbox: TaskInbox, scope: OwnerScope, attempt_id: str,
                      *, available: bool, grants: tuple[ActionGrant, ...]) -> TransferAttempt:
    """Release a *ready ticket* for a future runner; never launch a model."""
    if (not isinstance(inbox, TaskInbox) or inbox._store is None
            or not isinstance(scope, OwnerScope) or type(available) is not bool
            or type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id)):
        raise TransferDenied("invalid_transfer_activation")
    with inbox._lock:
        db = inbox._store._db()
        row = _approval_context_locked(inbox,scope,attempt_id,grants,now=time.time())
        if row[4] != "waiting" or not available:
            raise TransferDenied("agent_unavailable")
        # This is a durable *ready* receipt, not a process-execution grant.
        try:
            with db:
                updated = db.execute("""UPDATE task_transfer_attempts SET state='ready'
                    WHERE attempt_id=? AND state='waiting'""",(attempt_id,))
                if updated.rowcount != 1:
                    raise TransferDenied("attempt_changed")
                db.execute("""INSERT INTO task_transfer_audit
                    (task_id,proposal_id,attempt_id,action,outcome,reason_code)
                    VALUES (?,?,?,'activate','allowed','ready_no_process')""",
                    (row[0],row[5],attempt_id))
        except sqlite3.Error as exc:
            raise StorageUnavailable("cannot persist transfer readiness") from exc
        return TransferAttempt(row[0],attempt_id,row[1],row[2],row[3],"ready")


def observe_unavailable(inbox: TaskInbox, scope: OwnerScope,
                        attempt_id: str, policy: PriorityWaitPolicy,
                        *, grants: tuple[ActionGrant, ...],
                        eta_seconds: float | None = None) -> WaitAdvice:
    """Persist an unavailable poll; only *suggest* a different candidate.

    Numeric limits are supplied by an external trusted, owner-approved policy
    after 7.11/7.12; no built-in production time thresholds exist.
    """
    if (not isinstance(policy,PriorityWaitPolicy)
            or type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id)
            or eta_seconds is not None and
               (type(eta_seconds) not in (int,float) or
                not 0 <= eta_seconds <= 604800)):
        raise TransferDenied("invalid_wait_observation")
    now = time.time()
    with inbox._lock:
        row = _approval_context_locked(inbox,scope,attempt_id,grants,now=now)
        record = inbox._scoped_record(row[0],scope)
        if (row[4] != "waiting" or policy.owner_id != scope.owner_id
                or policy.project_id != record.project_id or row[10] is None):
            raise TransferDenied("wait_policy_or_state_mismatch")
        tier = next((t for t in reversed(policy.tiers)
                     if inbox._priorities[record.task_id] >= t.minimum_priority),None)
        if tier is None or now < row[10]-5:
            raise TransferDenied("invalid_wait_clock")
        elapsed = max(0.,now-row[10])
        checks = min(row[11]+1,tier.maximum_checks)
        advice = ("suggest_alternative" if elapsed >= tier.maximum_seconds
                  or checks >= tier.maximum_checks
                  or eta_seconds is not None and
                     eta_seconds > tier.maximum_seconds-elapsed
                  else "wait")
        db = inbox._store._db()
        with db:
            updated = db.execute("""UPDATE task_transfer_attempts
                SET wait_checks=? WHERE attempt_id=? AND state='waiting'
                AND wait_checks=?""",(checks,attempt_id,row[11]))
            if updated.rowcount != 1:
                raise TransferDenied("wait_state_changed")
            db.execute("""INSERT INTO task_transfer_audit
                (task_id,proposal_id,attempt_id,action,outcome,reason_code)
                VALUES (?,?,?,'availability_poll','held',?)""",
                (row[0],row[5],attempt_id,advice))
        return WaitAdvice(attempt_id,advice,checks,elapsed)


def transfer_history(inbox: TaskInbox, scope: OwnerScope,
                     task_id: str) -> tuple[TransferAttempt, ...]:
    if not isinstance(inbox,TaskInbox) or not isinstance(scope,OwnerScope):
        raise TransferDenied("invalid_history_scope")
    with inbox._lock:
        inbox._scoped_record(task_id,scope)
        if inbox._store is None:
            return ()
        rows = inbox._store._db().execute("""SELECT task_id,attempt_id,ordinal,
            agent_id,provider_id,state FROM task_transfer_attempts
            WHERE task_id=? ORDER BY ordinal""",(task_id,)).fetchall()
        return tuple(TransferAttempt(*r) for r in rows)


def transfer_artifacts(inbox: TaskInbox, scope: OwnerScope,
                       attempt_id: str) -> tuple[CheckpointFile, ...]:
    """Owner-scoped checkpoint read; enforce persisted SHA on every byte body."""
    if type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id):
        raise TransferDenied("invalid_artifact_identifier")
    with inbox._lock:
        db = inbox._store._db()
        row = db.execute("SELECT task_id FROM task_transfer_attempts WHERE attempt_id=?",
                         (attempt_id,)).fetchone()
        if row is None:
            raise TransferDenied("attempt_unavailable")
        inbox._scoped_record(row[0],scope)
        items = db.execute("""SELECT path,sha256,status,evidence_sha256,body
            FROM task_transfer_artifacts WHERE attempt_id=? ORDER BY path""",
            (attempt_id,)).fetchall()
        result = []
        for path,digest,status,evidence,body in items:
            item = CheckpointFile(path,bytes(body),evidence)
            if item.sha256 != digest or item.status != status:
                raise StorageUnavailable("checkpoint integrity mismatch")
            result.append(item)
        return tuple(result)
