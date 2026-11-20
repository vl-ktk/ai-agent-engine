"""Isolated 7.11 CPU/provider admission E2E over approved personal adapters.

Default OFFLINE mode starts only disposable deterministic subprocesses.
LIVE mode requires explicit owner approval and uses exactly one existing
subscription request for GPT and one for Claude. No persistent daemon, API
key fallback, production SQLite access, account login or returned text logs.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile

from .main_orchestrator import OwnerDecision
from .main_plan import approve_plan, decompose_for_review
from .main_shared_capacity import CapacityPolicy, ProviderBudget, SharedCapacity
from .main_specialists import (
    TrustedStepRequirements, approve_catalog, approve_team, assign_specialist,
    draft_catalog, draft_team, parse_candidate,
)
from .task_api import OwnerScope, TaskInbox, TaskSubmission
from .task_assignment import WorkspaceRoot
from .task_claude import ClaudeTaskExecutor
from .task_gpt import GPTTaskExecutor
from .task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from .task_state import TaskState
from .task_storage import SQLiteTaskStore, SCHEMA_VERSION


PROJECT = "engine-711-private"
OWNER = OwnerScope("owner-711-private", frozenset({PROJECT}))
STEP = "probe"
MARKERS = {"gpt": "AE-711-GPT", "claude": "AE-711-CLAUDE"}
AGENTS = ("gpt_hold", "gpt_real", "gpt_quota", "claude_real")
OFFLINE_WORKER = (
    "import json,sys;"
    "request=json.loads(sys.stdin.buffer.read());"
    "print(json.dumps({'ok':True,'text':request['prompt']}),flush=True)"
)


class AcceptanceBlocked(RuntimeError):
    """Only sanitized stage acceptance failure reaches the caller."""


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _decision(proposal) -> OwnerDecision:
    return OwnerDecision(proposal.owner_id, proposal.proposal_id,
                         proposal.sha256, "button")


class _ReviewedPlan:
    """Trusted fixed fixture, never supplied or revised by the AI model."""

    def propose(self, source, feedback, detail_hint):
        return _json({"schema_version": 1, "steps": [{
            "step_id": STEP, "project_id": PROJECT,
            "title": "Check private provider admission",
            "goal": "Verify one shared admission gate",
            "instructions": "Answer only the synthetic marker",
            "boundaries": "No tools, files or production writes",
            "expected_result": "One sanitized marker reply",
            "acceptance_checks": ["Exact response marker"],
            "expected_artifacts": ["Transient audit receipt"],
            "depends_on": [], "parent_step_id": None,
        }]})

    def review(self, source, plan_json):
        return _json({"plan_sha256": sha256(plan_json).hexdigest(),
                      "passed": True, "summary": "Fixed private probe",
                      "findings": []})


def _approve(inbox: TaskInbox) -> str:
    """Create exact owner-approved model/role/team/step fixtures in scratch."""
    role = {
        "role_id": "private_probe", "name": "Private provider probe",
        "instructions": "Return one marker without tools",
        "boundaries": "No workspace mutation or API fallback",
        "skills": ["private_probe"], "tools": ["unit_test"],
        "actions": ["external_request"],
        "primary_provider": "gpt", "fallback_providers": ["claude"],
    }
    proposed_cat = draft_catalog(OWNER, _json({
        "schema_version": 1, "roles": [role],
    }))
    catalog = approve_catalog(inbox, OWNER, proposed_cat, _decision(proposed_cat))
    proposed_team = draft_team(OWNER, _json({
        "schema_version": 1, "project_id": PROJECT,
        "catalog_revision": catalog.proposal.revision,
        "catalog_sha256": catalog.proposal.sha256, "local_roles": [],
        "members": [{
            "agent_id": agent, "role_id": "private_probe",
            "workspace_name": agent, "actions": ["external_request"],
        } for agent in AGENTS],
        "role_limits": [{"role_id": "private_probe", "max_instances": 4}],
    }), catalog, team_id="team_capacity_711", revision=1)
    approve_team(inbox, OWNER, proposed_team, _decision(proposed_team))
    proposed_plan = decompose_for_review(_ReviewedPlan(), OWNER,
        TaskSubmission(PROJECT, "Private 7.11 review", "Approve fixed probes"))
    approve_plan(inbox, OWNER, proposed_plan.proposal,
                 _decision(proposed_plan.proposal))
    return proposed_plan.proposal.plan_id


def _assign(inbox: TaskInbox, plan_id: str, provider: str,
            worker: str, title: str) -> str:
    prompt = "Reply with only the marker " + MARKERS[provider] + "."
    task = inbox.accept(TaskSubmission(PROJECT, title, prompt), OWNER).task_id
    action = ProtectedAction.EXTERNAL_REQUEST
    assign_specialist(inbox, OWNER,
        TrustedStepRequirements(task, plan_id, STEP, ("private_probe",), (action,)),
        "team_capacity_711", parse_candidate(_json({
            "agent_id": worker, "provider_id": provider,
            "fallback_reason": "approved_fallback" if provider == "claude" else None,
        })), grants=(ActionGrant(task, worker, action),))
    return task


def _runner(inbox: TaskInbox, capacity: SharedCapacity, task: str,
            provider: str, worker: str, live: bool) -> GPTTaskExecutor:
    options = dict(worker_id=worker, capacity=capacity,
                   permission_gate=TaskPermissionGate(inbox, grants=(
                       ActionGrant(task, worker, ProtectedAction.EXTERNAL_REQUEST),)),
                   model_id="gpt-6.1-sol" if provider == "gpt" else "default",
                   timeout_seconds=100 if provider == "gpt" else 210)
    if not live:
        options["worker_argv"] = (sys.executable, "-u", "-c", OFFLINE_WORKER)
    if provider == "gpt":
        return GPTTaskExecutor(inbox, OWNER, **options)
    return ClaudeTaskExecutor(inbox, OWNER,
                              authorized_owner_id=OWNER.owner_id, **options)


def run_acceptance(*, live: bool = False, owner_approved: bool = False) -> dict:
    if live and not owner_approved:
        raise AcceptanceBlocked("explicit_owner_approval_required")
    with tempfile.TemporaryDirectory(prefix="ae-711-private-") as dirname:
        root = Path(dirname)
        workspaces = root / "workspaces"
        workspaces.mkdir(mode=0o700)
        for agent in AGENTS:
            (workspaces / agent).mkdir(mode=0o700)
        db_path = root / "tasks.sqlite3"
        inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                          workspace_root=WorkspaceRoot(workspaces))
        runners = []
        try:
            if SCHEMA_VERSION != 17:
                raise AcceptanceBlocked("unexpected_task_schema")
            plan_id = _approve(inbox)
            hold = _assign(inbox, plan_id, "gpt", "gpt_hold", "Synthetic CPU holder")
            gpt = _assign(inbox, plan_id, "gpt", "gpt_real", "Approved GPT request")
            claude = _assign(inbox, plan_id, "claude", "claude_real", "Approved Claude request")
            quota = _assign(inbox, plan_id, "gpt", "gpt_quota", "GPT quota waiting")
            # Isolated acceptance limits only: no production policy change.
            capacity = SharedCapacity(inbox, CapacityPolicy(1, (
                ProviderBudget("gpt", 1, 2, 300),
                ProviderBudget("claude", 1, 1, 300),
            )))
            gpt_runner = _runner(inbox, capacity, gpt, "gpt", "gpt_real", live)
            claude_runner = _runner(inbox, capacity, claude, "claude", "claude_real", live)
            quota_runner = _runner(inbox, capacity, quota, "gpt", "gpt_quota", live)
            runners.extend((gpt_runner, claude_runner, quota_runner))

            holding = capacity.acquire(hold, OWNER, worker_id="gpt_hold")
            if not holding.admitted:
                raise AcceptanceBlocked("initial_cpu_slot_unavailable")
            waiting = claude_runner.execute(claude, "claude_real")
            if (waiting.executed or waiting.state is not TaskState.ASSIGNED
                    or waiting.capacity_reason != "cpu_busy"
                    or claude_runner.delivery.snapshot(claude, OWNER) is not None
                    or inbox._store.process_record(claude) is not None):
                raise AcceptanceBlocked("cpu_wait_did_not_block_execution")
            inbox.transition_state(hold, TaskState.FAILED, OWNER,
                                   actor_id="gpt_hold")
            if not capacity.release(hold, OWNER, worker_id="gpt_hold",
                                    reservation_id=holding.reservation_id):
                raise AcceptanceBlocked("synthetic_slot_not_released")

            for provider, task, runner in (("gpt", gpt, gpt_runner),
                                           ("claude", claude, claude_runner)):
                outcome = runner.execute(task, provider + "_real")
                if (outcome.state is not TaskState.SUCCEEDED or not outcome.executed
                        or outcome.text is None
                        or MARKERS[provider] not in outcome.text
                        or runner.delivery.snapshot(task, OWNER).status != "completed"
                        or [(e.provider, e.outcome) for e in
                            runner.provider_history(task)] != [(provider, "completed")]
                        or inbox._store.capacity_reservation(task) is not None
                        or inbox._store.process_record(task)[-1] != "stopped"):
                    raise AcceptanceBlocked("approved_provider_e2e_failed")

            deferred = quota_runner.execute(quota, "gpt_quota")
            if (deferred.executed or deferred.capacity_reason != "provider_quota"
                    or inbox._store.process_record(quota) is not None
                    or quota_runner.delivery.snapshot(quota, OWNER) is not None):
                raise AcceptanceBlocked("rolling_provider_quota_bypassed")
            for runner in runners:
                runner.close()
            runners.clear()
            inbox.close()
            inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                              workspace_root=WorkspaceRoot(workspaces))
            if (inbox.state(gpt, OWNER) is not TaskState.SUCCEEDED
                    or inbox.state(claude, OWNER) is not TaskState.SUCCEEDED
                    or inbox.state(quota, OWNER) is not TaskState.ASSIGNED
                    or inbox._store.capacity_reservation(gpt) is not None
                    or inbox._store.capacity_reservation(claude) is not None
                    or inbox._store._db().execute("PRAGMA user_version").fetchone()
                    != (17,)):
                raise AcceptanceBlocked("durable_capacity_recovery_failed")
            return {"schema": 1, "status": "LIVE_PASS" if live else "OFFLINE_PASS",
                    "providers": ["gpt", "claude"],
                    "cpu_slot_wait": "PASS", "provider_quota_wait": "PASS",
                    "delivery_not_spent_on_wait": True,
                    "durable_sqlite_v17": True,
                    "live_provider_calls": 2 if live else 0,
                    "production_db_modified": False,
                    "production_release_changed": False,
                    "provider_text_printed": False}
        finally:
            for runner in runners:
                runner.close()
            inbox.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Disposable 7.11 capacity E2E")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--owner-approved", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(live=args.live, owner_approved=args.owner_approved)
    except Exception:
        print("STAGE7_11_" + ("LIVE" if args.live else "OFFLINE")
              + " BLOCKED: private acceptance failed; no output disclosed")
        return 2
    print("STAGE7_11_OWNER_E2E " + json.dumps(result, sort_keys=True,
                                               separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
