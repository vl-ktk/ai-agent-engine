"""7.8: one-shot crisis delegation inside the existing approved task system.

Offline trusted coordinator only: it never calls models, issues grants, changes
the owning worker or sends external messages. Each incident has at most one
durably bound child task. The child may run only through the normal 7.3
approved-specialist matcher with its own task-specific expiring grants.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import time
from typing import Callable

from .main_specialists import (
    SpecialistCandidate, TrustedStepRequirements, assign_specialist,
    load_catalog, load_specialist_pin, load_team,
)
from .project_diagnostics import ProjectDiagnosticsStore
from .task_api import OwnerScope, TaskInbox, TaskSubmission, _TASK_ID
from .task_permissions import ActionGrant
from .task_state import TaskState, TERMINAL_STATES


class CrisisDenied(PermissionError):
    """Safe denial: no prompts, secrets or foreign project data."""


@dataclass(frozen=True, slots=True)
class CrisisEscalation:
    incident_id: str
    owner_id: str
    project_id: str
    parent_task_id: str
    child_task_id: str
    original_agent: str
    crisis_agent: str
    crisis_provider: str
    crisis_role: str
    plan_sha256: str
    team_sha256: str
    catalog_sha256: str
    step_id: str
    manifest_sha256: str
    stop_reason: str
    deadline_at: float
    status: str
    outcome_sha256: str | None
    reason_code: str | None


def _digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
                  .encode("utf-8")).hexdigest()


class CrisisEscalationCoordinator:
    """Durable 7.8 state machine; only trusted server-side Python may invoke it.

    Role/plan/team authorizations already exist before an incident. Quotas are
    a supplied approved time cap, never a permission to buy provider credits.
    All ordinary task execution/OS isolation is handled by existing runtime.
    """

    def __init__(self, inbox: TaskInbox, diagnostics: ProjectDiagnosticsStore,
                 scope: OwnerScope, project_id: str) -> None:
        if (not isinstance(inbox, TaskInbox) or inbox._store is None
                or not isinstance(diagnostics, ProjectDiagnosticsStore)
                or not isinstance(scope, OwnerScope)
                or type(project_id) is not str
                or project_id not in scope.project_ids):
            raise CrisisDenied("unapproved_crisis_context")
        self.inbox = inbox
        self.diagnostics = diagnostics
        self.scope = scope
        self.project_id = project_id

    def _read_locked(self, incident_id: str) -> CrisisEscalation | None:
        row = self.inbox._store.crisis_row(incident_id)
        if row is None:
            return None
        record = CrisisEscalation(*row)
        if (record.owner_id != self.scope.owner_id
                or record.project_id != self.project_id):
            raise CrisisDenied("crisis_scope_denied")
        parent = self.inbox._scoped_record(record.parent_task_id, self.scope)
        child = self.inbox._scoped_record(record.child_task_id, self.scope)
        if (parent.project_id != self.project_id
                or child.project_id != self.project_id
                or child.parent_task_id != parent.task_id):
            raise CrisisDenied("crisis_binding_corrupted")
        return record

    def read(self, incident_id: str) -> CrisisEscalation | None:
        with self.inbox._lock:
            return self._read_locked(incident_id)

    def reserve(self, incident_id: str, requirements: TrustedStepRequirements,
                candidate: SpecialistCandidate, *, team_id: str,
                crisis_role: str, stop_reason: str,
                max_seconds: int) -> CrisisEscalation:
        """Reserve one narrowly scoped child, before any model side effect.

        A supervisor can stop early after a distinct FAILED diagnostic attempt.
        HOLD due to unknown process/QA never becomes an automatic new attempt.
        """
        if (not isinstance(requirements, TrustedStepRequirements)
                or not isinstance(candidate, SpecialistCandidate)
                or type(crisis_role) is not str
                or type(stop_reason) is not str
                or stop_reason not in ("stalled", "exhausted")
                or type(max_seconds) is not int
                or not 1 <= max_seconds <= 3600):
            raise CrisisDenied("invalid_crisis_request")
        incident = self.diagnostics.incident(self.scope, self.project_id,
                                              incident_id)
        attempts = self.diagnostics.attempts(self.scope, self.project_id,
                                              incident_id)
        if (incident.task_id != requirements.task_id
                or not 1 <= len(attempts) <= 3
                or (stop_reason == "stalled" and
                    (incident.status != "OPEN" or
                     any(a.status != "FAILED" for a in attempts)))
                or (stop_reason == "exhausted" and
                    (len(attempts) != 3 or incident.status != "HOLD"
                     or any(a.status != "FAILED" for a in attempts[:2])
                     or attempts[2].status != "HOLD"))):
            raise CrisisDenied("diagnostic_not_eligible")
        with self.inbox._lock:
            parent = self.inbox._scoped_record(incident.task_id, self.scope)
            if (parent.project_id != self.project_id
                    or self.inbox._states[parent.task_id] not in
                    (TaskState.ASSIGNED, TaskState.RUNNING)
                    or self.inbox._store.crisis_child(parent.task_id)
                    or self.inbox._store.transfer_has_started(parent.task_id)
                    or self.inbox._store.emergency_held(self.scope.owner_id,
                                                        self.project_id)):
                raise CrisisDenied("crisis_parent_unavailable")
            process = self.inbox._store.process_record(parent.task_id)
            if ((process is not None and process[7] != "stopped")
                    or (self.inbox._states[parent.task_id] is TaskState.RUNNING
                        and process is None)):
                raise CrisisDenied("crisis_parent_process_active")
            original = load_specialist_pin(self.inbox, self.scope,
                                           parent.task_id)
            team = load_team(self.inbox, self.scope, team_id)
            if (original is None or team is None
                    or original.plan_id != requirements.plan_id
                    or original.step_id != requirements.step_id
                    or original.team_id != team_id
                    or original.team_digest != team.proposal.sha256
                    or original.agent_id == candidate.agent_id
                    or not set(requirements.actions).issubset(
                        original.allowed_actions)):
                raise CrisisDenied("crisis_not_in_approved_plan")
            catalog = load_catalog(self.inbox, self.scope,
                                   team.proposal.spec.catalog_revision)
            if catalog is None or catalog.proposal.sha256 != original.catalog_digest:
                raise CrisisDenied("crisis_catalog_stale")
            member = next((m for m in team.proposal.spec.members
                           if m.agent_id == candidate.agent_id), None)
            profile = next((r for r in (*catalog.proposal.spec.roles,
                                       *team.proposal.spec.local_roles)
                            if r.role_id == crisis_role), None)
            if (member is None or member.role_id != crisis_role
                    or crisis_role == original.role_id or profile is None
                    or not set(requirements.skills).issubset(profile.skills)
                    or not set(requirements.actions).issubset(member.actions)
                    or member.workspace_name ==
                    self.inbox._assignments[parent.task_id].workspace_name
                    or (candidate.provider_id != profile.primary_provider
                        and (candidate.provider_id not in profile.fallback_providers
                             or candidate.fallback_reason is None))
                    or (candidate.provider_id == profile.primary_provider
                        and candidate.fallback_reason is not None)):
                raise CrisisDenied("crisis_specialist_not_approved")
            manifest = _digest((incident.fingerprint, parent.task_id,
                                original.plan_digest, original.team_digest,
                                original.catalog_digest, requirements.step_id,
                                requirements.skills,
                                [action.value for action in requirements.actions],
                                candidate.agent_id, candidate.provider_id,
                                candidate.fallback_reason, crisis_role,
                                stop_reason, max_seconds))
            old = self._read_locked(incident_id)
            if old is not None:
                if old.manifest_sha256 != manifest:
                    raise CrisisDenied("crisis_already_reserved")
                return old
            # A keyed child admission may have committed before a process
            # crash. Reusing the key recovers that exact child, not another.
            child = self.inbox.accept(TaskSubmission(
                self.project_id, "Narrow crisis diagnostic",
                f"Investigate approved incident {incident_id} within the pinned "
                "plan. No new grants, external effects or deployment.",
                parent_task_id=parent.task_id), self.scope,
                idempotency_key="crisis_v1:" + incident_id)
            deadline = time.time() + max_seconds
            self.inbox._store.insert_crisis((
                incident_id,self.scope.owner_id,self.project_id,parent.task_id,
                child.task_id,original.agent_id,candidate.agent_id,
                candidate.provider_id,crisis_role,original.plan_digest,
                original.team_digest,original.catalog_digest,
                requirements.step_id,manifest,stop_reason,deadline),
                _digest((incident.fingerprint, [a.attempt_id for a in attempts])))
            return self._read_locked(incident_id)

    def activate(self, incident_id: str, requirements: TrustedStepRequirements,
                 candidate: SpecialistCandidate, *, team_id: str,
                 grants: tuple[ActionGrant, ...]) -> CrisisEscalation:
        """One authorized 7.3 assignment; never creates or inherits grants."""
        if (not isinstance(requirements, TrustedStepRequirements)
                or not isinstance(candidate, SpecialistCandidate)
                or type(grants) is not tuple
                or any(not isinstance(g, ActionGrant) for g in grants)):
            raise CrisisDenied("invalid_crisis_activation")
        with self.inbox._lock:
            record = self._read_locked(incident_id)
            if record is None:
                raise CrisisDenied("crisis_not_reserved")
            parent_pin = load_specialist_pin(
                self.inbox,self.scope,record.parent_task_id)
            if (parent_pin is None
                    or parent_pin.plan_digest != record.plan_sha256
                    or parent_pin.team_digest != record.team_sha256
                    or parent_pin.catalog_digest != record.catalog_sha256
                    or candidate.agent_id != record.crisis_agent
                    or candidate.provider_id != record.crisis_provider
                    or team_id != parent_pin.team_id
                    or requirements.task_id != record.child_task_id
                    or requirements.step_id != record.step_id
                    or requirements.plan_id != parent_pin.plan_id):
                raise CrisisDenied("crisis_activation_denied")
            if record.status == "assigned":
                return record  # Idempotent read; never reassign or reissue a token.
            if (record.status != "reserved" or time.time() >= record.deadline_at
                    or len(grants) != len(requirements.actions)
                    or {g.action for g in grants} != set(requirements.actions)
                    or not set(requirements.actions).issubset(
                        parent_pin.allowed_actions)
                    or any(g.task_id != record.child_task_id
                           or g.worker_id != record.crisis_agent
                           or g.expires_at is None
                           or g.expires_at > record.deadline_at
                           for g in grants)):
                raise CrisisDenied("crisis_activation_denied")
            pinned = load_specialist_pin(self.inbox,self.scope,record.child_task_id)
            if pinned is None:
                assign_specialist(self.inbox,self.scope,requirements,team_id,
                                  candidate,grants=grants)
            elif (pinned.agent_id != record.crisis_agent
                  or pinned.provider_id != record.crisis_provider
                  or pinned.role_id != record.crisis_role):
                raise CrisisDenied("crisis_pin_corrupted")
            self.inbox._store.advance_crisis(
                incident_id,from_status="reserved",to_status="assigned",
                outcome_sha256=_digest((record.child_task_id,
                                        record.crisis_agent,record.crisis_provider)),
                reason_code="one_crisis_assignment")
            return self._read_locked(incident_id)

    def conclude(self, incident_id: str, *,
                 trusted_qa: Callable[[], bool] | None = None) -> CrisisEscalation:
        """Verify one completed child, else HOLD and prepare owner report.

        A success requires the existing source-version receipt and an
        independent trusted QA callback. A failed/unknown outcome is never
        automatically retried or re-routed to another crisis specialist.
        """
        with self.inbox._lock:
            record = self._read_locked(incident_id)
            if record is None or record.status != "assigned":
                raise CrisisDenied("crisis_not_running")
            state = self.inbox._states[record.child_task_id]
            if state not in TERMINAL_STATES:
                raise CrisisDenied("crisis_child_not_terminal")
            version = self.inbox._store.read_source_version(record.child_task_id)
            passed = False
            if state is TaskState.SUCCEEDED and version is not None and callable(trusted_qa):
                try:
                    passed = (version[0] == record.crisis_agent
                              and (version[1] != version[2] or version[3] is not None)
                              and trusted_qa() is True)
                except Exception:
                    passed = False
            ready = state is TaskState.SUCCEEDED and passed
            self.inbox._store.advance_crisis(
                incident_id,from_status="assigned",
                to_status="ready_for_parent" if ready else "hold",
                outcome_sha256=_digest((state.value, version[:4] if version else None,
                                        "qa_pass" if ready else "qa_missing_or_failed")),
                reason_code="verified_result" if ready else "owner_review_required")
            return self._read_locked(incident_id)

    def recover_unknown(self, incident_id: str) -> CrisisEscalation:
        """On restart, fence an unresolved assigned child; no blind replay."""
        with self.inbox._lock:
            record = self._read_locked(incident_id)
            if record is None:
                raise CrisisDenied("crisis_not_found")
            if record.status == "assigned" and self.inbox._states[
                    record.child_task_id] not in TERMINAL_STATES:
                self.inbox._store.advance_crisis(
                    incident_id,from_status="assigned",to_status="hold",
                    outcome_sha256=_digest((record.child_task_id,"unknown_after_restart")),
                    reason_code="unknown_execution_owner_review")
            return self._read_locked(incident_id)

    def owner_report(self, incident_id: str) -> dict[str, object]:
        """Safe metadata for the *future* owner notifier, not an outgoing call."""
        with self.inbox._lock:
            row = self._read_locked(incident_id)
            if row is None:
                raise CrisisDenied("crisis_not_found")
            # These records contain independently validated codes and status,
            # not untrusted failure excerpts, prompts or credential values.
            occurrences = self.diagnostics.occurrences(
                self.scope,self.project_id,incident_id)
            attempts = self.diagnostics.attempts(
                self.scope,self.project_id,incident_id)
            return {"incident_id":row.incident_id,
                    "parent_task_id":row.parent_task_id,
                    "crisis_task_id":row.child_task_id,
                    "status":row.status,
                    "reason_code":row.reason_code,
                    "failure_code":occurrences[-1].error_code if occurrences else None,
                    "ordinary_attempts":[{"slot":attempt.slot,
                                          "status":attempt.status}
                                         for attempt in attempts],
                    "needs_owner":row.status == "hold",
                    "return_to_original":row.status == "ready_for_parent"}
