"""7.8: one-shot crisis task against real private SQLite, no provider calls."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from ai_agent_engine.main_escalation import CrisisDenied, CrisisEscalationCoordinator
from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.main_plan import approve_plan, decompose_for_review
from ai_agent_engine.main_specialists import (
    SpecialistDenied, TrustedStepRequirements, approve_catalog, approve_team,
    assign_specialist, draft_catalog, draft_team, parse_candidate,
)
from ai_agent_engine.project_diagnostics import (
    FailureSignal, ProjectDiagnosticsStore, capture_git_source,
)
from ai_agent_engine.project_leader import ProjectLeaderStore, draft_project
from ai_agent_engine.task_api import OwnerScope, TaskInbox, TaskSubmission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_delivery import TaskDeliveryManager, DeliveryRejected
from ai_agent_engine.task_permissions import ActionGrant, ProtectedAction
from ai_agent_engine.task_permissions import TaskPermissionGate, PermissionDenied
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore

from test_main_specialists import OnePlanModel, catalog, decision, encoded, role, team


SCOPE = OwnerScope("owner78", frozenset({"engine"}))
OTHER = OwnerScope("other78", frozenset({"engine"}))
EXECUTE = ProtectedAction.EXECUTE_TOOL


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private SQLite only")
class CrisisEscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-78-")
        base = Path(self.tmp.name)
        self.workspaces = base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        for name in ("slot-one", "slot-two"):
            (self.workspaces / name).mkdir(mode=0o700)
        self.db = base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                               workspace_root=WorkspaceRoot(self.workspaces))
        project_root = base / "projects"
        project_root.mkdir(mode=0o700)
        self.leaders = ProjectLeaderStore(project_root)
        project = draft_project(SCOPE,"engine","Engine")
        self.leaders.approve(SCOPE,project,OwnerDecision(
            SCOPE.owner_id,project.proposal_id,project.sha256,"button"))
        repo = base / "source"
        repo.mkdir(mode=0o700)
        self.repo = repo
        self.git("init","-q")
        (repo / "tracked.txt").write_text("baseline\n")
        self.git("add","--","tracked.txt")
        self.git("-c","user.name=Local QA","-c",
                 "user.email=qa@example.invalid","commit","-qm","base")
        self.base_sha = self.git("rev-parse","HEAD").strip()
        self.source = capture_git_source(repo,self.base_sha)
        self.diagnostics = ProjectDiagnosticsStore(self.leaders,
            source_bindings={(SCOPE.owner_id,"engine"):repo})
        normal = role()
        crisis = role("crisis",name="Approved crisis developer")
        c = draft_catalog(SCOPE,catalog(normal,crisis))
        cat = approve_catalog(self.inbox,SCOPE,c,decision(c))
        agents = [
            {"agent_id":"ui_one","role_id":"frontend","workspace_name":"slot-one",
             "actions":["write_workspace","execute_tool"]},
            {"agent_id":"crisis_one","role_id":"crisis","workspace_name":"slot-two",
             "actions":["write_workspace","execute_tool"]},
        ]
        t = draft_team(SCOPE,team(c,agents=agents,limits=[
            {"role_id":"frontend","max_instances":1},
            {"role_id":"crisis","max_instances":1}]),cat,team_id="team_engine")
        approve_team(self.inbox,SCOPE,t,decision(t))
        p = decompose_for_review(OnePlanModel(),SCOPE,
            TaskSubmission("engine","Build","Reviewed build")).proposal
        self.plan = approve_plan(self.inbox,SCOPE,p,decision(p))
        self.original = self.inbox.accept(TaskSubmission("engine","Build","Private"),
                                          SCOPE).task_id
        self.parent_req = self.requirements(self.original)
        assign_specialist(self.inbox,SCOPE,self.parent_req,"team_engine",
                          self.candidate("ui_one"),grants=(
                              ActionGrant(self.original,"ui_one",EXECUTE),))
        self.coordinator = CrisisEscalationCoordinator(
            self.inbox,self.diagnostics,SCOPE,"engine")

    def tearDown(self):
        self.inbox.close()
        self.leaders.close()
        self.tmp.cleanup()

    def git(self,*args):
        p = subprocess.run(["git","-C",str(self.repo),*args],
                           stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,timeout=10,text=True)
        if p.returncode:
            raise AssertionError("disposable Git fixture failed")
        return p.stdout

    def candidate(self,agent="crisis_one",provider="gpt"):
        return parse_candidate(encoded({"agent_id":agent,"provider_id":provider,
                                        "fallback_reason":None}))

    def requirements(self,task_id):
        return TrustedStepRequirements(task_id,self.plan.proposal.plan_id,
                                       "ui",("frontend",),(EXECUTE,))

    def failure(self):
        return self.diagnostics.record_failure(SCOPE,"engine",signal=FailureSignal(
            self.original,"test_failure","unit_error","unit.test", "OK", "FAIL",
            "Synthetic failing unit test"),source=self.source,delivery_key="fail78")

    def failed_attempt(self,incident,slot=1):
        attempt = self.diagnostics.reserve_attempt(
            SCOPE,"engine",incident_id=incident.incident_id,
            source=self.source,hypothesis=f"Distinct hypothesis {slot}",
            test_plan="Run regression",idempotency_key=f"attempt-{slot}")
        return self.diagnostics.verify_attempt(
            SCOPE,"engine",attempt_id=attempt.attempt_id,source=self.source,
            checks={"reproducer":lambda: False})

    def reserved(self,*,stop_reason="stalled",max_seconds=300):
        incident = self.failure()
        self.failed_attempt(incident)
        return self.coordinator.reserve(
            incident.incident_id,self.parent_req,self.candidate(),
            team_id="team_engine",crisis_role="crisis",
            stop_reason=stop_reason,max_seconds=max_seconds)

    def activate(self,record):
        return self.coordinator.activate(
            record.incident_id,self.requirements(record.child_task_id),
            self.candidate(),team_id="team_engine",grants=(ActionGrant(
                record.child_task_id,"crisis_one",EXECUTE,time.time()+50),))

    def test_reserve_creates_one_scoped_child_and_restart_preserves_audit(self):
        record = self.reserved()
        self.assertEqual(record.status,"reserved")
        self.assertEqual(record.parent_task_id,self.original)
        self.assertNotEqual(record.original_agent,record.crisis_agent)
        self.assertEqual(self.inbox.state(self.original,SCOPE),TaskState.ASSIGNED)
        self.assertEqual(self.inbox.snapshot(SCOPE).__len__(),2)
        self.assertEqual(self.coordinator.reserve(record.incident_id,self.parent_req,
            self.candidate(),team_id="team_engine",crisis_role="crisis",
            stop_reason="stalled",max_seconds=300),record)
        self.assertEqual([a[0] for a in self.inbox._store.crisis_audit(record.incident_id)],
                         ["reserved"])
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                               workspace_root=WorkspaceRoot(self.workspaces))
        self.coordinator = CrisisEscalationCoordinator(
            self.inbox,self.diagnostics,SCOPE,"engine")
        self.assertEqual(self.coordinator.read(record.incident_id),record)
        self.assertEqual(self.coordinator.owner_report(record.incident_id)
                         ["needs_owner"],False)

    def test_v14_to_v15_migration_preserves_permission_history(self):
        self.inbox._store.write_permission_audit(
            self.original,EXECUTE.value,False,"grant_missing","assigned_worker")
        self.inbox.close()
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA user_version=14")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                               workspace_root=WorkspaceRoot(self.workspaces))
        self.coordinator = CrisisEscalationCoordinator(
            self.inbox,self.diagnostics,SCOPE,"engine")
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(),(15,))
        self.assertEqual(self.inbox._store.permission_history(self.original)
                         [0][4],"grant_missing")
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT COUNT(*) FROM task_crisis_escalations").fetchone(),(0,))

    def test_activation_separate_task_grants_and_one_delivery_attempt(self):
        record = self.reserved()
        req = self.requirements(record.child_task_id)
        for bad in ((),(ActionGrant(record.parent_task_id,"ui_one",EXECUTE),),
                    (ActionGrant(record.child_task_id,"crisis_one",EXECUTE),)):
            with self.assertRaises((CrisisDenied,SpecialistDenied)):
                self.coordinator.activate(record.incident_id,req,self.candidate(),
                    team_id="team_engine",grants=bad)
        ready = self.activate(record)
        self.assertEqual(ready.status,"assigned")
        self.assertEqual(self.inbox.state(ready.child_task_id,SCOPE),TaskState.ASSIGNED)
        delivery = TaskDeliveryManager(self.inbox)
        with self.assertRaises(DeliveryRejected):
            delivery.reserve(ready.child_task_id,SCOPE,actor_id="crisis_one",
                             max_attempts=3)
        ticket = delivery.reserve(ready.child_task_id,SCOPE,
                                  actor_id="crisis_one",max_attempts=1)
        self.assertTrue(ticket.should_execute)
        self.assertFalse(delivery.reserve(ready.child_task_id,SCOPE,
                                         actor_id="crisis_one",max_attempts=1)
                         .should_execute)

    def test_failed_crisis_gives_owner_hold_never_second_agent(self):
        ready = self.activate(self.reserved())
        self.inbox.transition_state(ready.child_task_id,TaskState.FAILED,SCOPE,
                                    actor_id="crisis_one")
        held = self.coordinator.conclude(ready.incident_id)
        self.assertEqual(held.status,"hold")
        self.assertEqual(self.coordinator.owner_report(ready.incident_id),{
            "incident_id":held.incident_id,"parent_task_id":held.parent_task_id,
            "crisis_task_id":held.child_task_id,"status":"hold",
            "reason_code":"owner_review_required",
            "failure_code":"unit_error",
            "ordinary_attempts":[{"slot":1,"status":"FAILED"}],
            "needs_owner":True,
            "return_to_original":False})
        with self.assertRaises(CrisisDenied):
            self.coordinator.activate(held.incident_id,
                self.requirements(held.child_task_id),self.candidate(),
                team_id="team_engine",grants=())
        with self.assertRaises(CrisisDenied):
            self.coordinator.reserve(held.incident_id,self.parent_req,
                self.candidate(),team_id="team_engine",crisis_role="crisis",
                stop_reason="stalled",max_seconds=200)

    def test_unknown_restart_holds_without_restarting_worker(self):
        ready = self.activate(self.reserved())
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                               workspace_root=WorkspaceRoot(self.workspaces))
        self.coordinator = CrisisEscalationCoordinator(
            self.inbox,self.diagnostics,SCOPE,"engine")
        self.assertEqual(self.coordinator.recover_unknown(ready.incident_id).status,
                         "hold")
        self.assertEqual(self.coordinator.recover_unknown(ready.incident_id).status,
                         "hold")
        self.assertEqual(len(self.inbox._store.crisis_audit(ready.incident_id)),3)

    def test_hold_fences_a_still_running_workers_grants(self):
        ready = self.activate(self.reserved())
        self.inbox.transition_state(ready.child_task_id,TaskState.RUNNING,SCOPE,
                                    actor_id="crisis_one")
        granted = ActionGrant(ready.child_task_id,"crisis_one",EXECUTE,
                              time.time()+50)
        gate = TaskPermissionGate(self.inbox,grants=(granted,))
        self.assertEqual(self.coordinator.recover_unknown(ready.incident_id).status,
                         "hold")
        with self.assertRaises(PermissionDenied):
            gate.run(ready.child_task_id,SCOPE,actor_id="crisis_one",
                     action=EXECUTE,operation=lambda: self.fail("grant escaped HOLD"))
        with self.assertRaises(DeliveryRejected):
            TaskDeliveryManager(self.inbox).reserve(
                ready.child_task_id,SCOPE,actor_id="crisis_one",max_attempts=1)
        self.assertEqual(self.inbox._store.permission_history(ready.child_task_id)
                         [-1][4],"crisis_fenced")

    def test_expired_reservation_wont_dispatch(self):
        record = self.reserved(max_seconds=1)
        from unittest.mock import patch
        with patch("ai_agent_engine.main_escalation.time.time",
                   return_value=record.deadline_at+1):
            with self.assertRaises(CrisisDenied):
                self.activate(record)
        self.assertEqual(self.coordinator.read(record.incident_id).status,
                         "reserved")

    def test_failed_crisis_cannot_escalate_itself(self):
        ready = self.activate(self.reserved())
        incident = self.diagnostics.record_failure(
            SCOPE,"engine",signal=FailureSignal(
                ready.child_task_id,"test_failure","child_failure",
                "unit.child","OK","FAIL","Synthetic child defect"),
            source=self.source,delivery_key="child-failure")
        attempt = self.diagnostics.reserve_attempt(
            SCOPE,"engine",incident_id=incident.incident_id,source=self.source,
            hypothesis="Child failure hypothesis",test_plan="QA",
            idempotency_key="child-attempt")
        self.diagnostics.verify_attempt(SCOPE,"engine",attempt_id=attempt.attempt_id,
            source=self.source,checks={"reproducer":lambda:False})
        with self.assertRaises(CrisisDenied):
            self.coordinator.reserve(incident.incident_id,
                self.requirements(ready.child_task_id),self.candidate("ui_one"),
                team_id="team_engine",crisis_role="frontend",
                stop_reason="stalled",max_seconds=30)

    def test_early_escalation_requires_failed_attempt_and_no_unknowns(self):
        incident = self.failure()
        with self.assertRaises(CrisisDenied):
            self.coordinator.reserve(incident.incident_id,self.parent_req,
                self.candidate(),team_id="team_engine",crisis_role="crisis",
                stop_reason="stalled",max_seconds=300)
        self.diagnostics.reserve_attempt(
            SCOPE,"engine",incident_id=incident.incident_id,source=self.source,
            hypothesis="Unresolved effect",test_plan="QA",idempotency_key="a1")
        self.diagnostics.recover_unknown(SCOPE,"engine")
        with self.assertRaises(CrisisDenied):
            self.coordinator.reserve(incident.incident_id,self.parent_req,
                self.candidate(),team_id="team_engine",crisis_role="crisis",
                stop_reason="stalled",max_seconds=300)

    def test_exhaustion_three_diagnostic_attempts(self):
        incident = self.failure()
        for n in range(1,4):
            attempt = self.failed_attempt(incident,n)
        self.assertEqual(attempt.status,"HOLD")
        ready = self.coordinator.reserve(
            incident.incident_id,self.parent_req,self.candidate(),
            team_id="team_engine",crisis_role="crisis",
            stop_reason="exhausted",max_seconds=120)
        self.assertEqual(ready.stop_reason,"exhausted")
        self.assertEqual(ready.status,"reserved")

    def test_cross_owner_wrong_agent_and_nested_crisis_denied(self):
        ready = self.activate(self.reserved())
        with self.assertRaises(CrisisDenied):
            other = CrisisEscalationCoordinator(self.inbox,self.diagnostics,OTHER,"engine")
            other.read(ready.incident_id)
        with self.assertRaises(CrisisDenied):
            self.coordinator.reserve(ready.incident_id,self.parent_req,
                self.candidate("ui_one"),team_id="team_engine",
                crisis_role="frontend",stop_reason="stalled",max_seconds=300)

    def test_success_requires_verified_source_and_independent_qa(self):
        ready = self.activate(self.reserved())
        delivery = TaskDeliveryManager(self.inbox)
        ticket = delivery.reserve(ready.child_task_id,SCOPE,
                                  actor_id="crisis_one",max_attempts=1)
        self.inbox.transition_state(ready.child_task_id,TaskState.RUNNING,SCOPE,
                                    actor_id="crisis_one")
        self.inbox.transition_state(ready.child_task_id,TaskState.SUCCEEDED,SCOPE,
                                    actor_id="crisis_one")
        delivery.report(ready.child_task_id,SCOPE,actor_id="crisis_one",
                        token=ticket.token,success=True)
        self.inbox._store.write_source_version(
            ready.child_task_id,"crisis_one","a"*40,"b"*40,None)
        final = self.coordinator.conclude(ready.incident_id,trusted_qa=lambda:True)
        self.assertEqual(final.status,"ready_for_parent")
        self.assertTrue(self.coordinator.owner_report(ready.incident_id)
                        ["return_to_original"])
        self.assertEqual(self.inbox.state(self.original,SCOPE),TaskState.ASSIGNED)
        self.assertEqual([entry[0] for entry in self.inbox._store.crisis_audit(
                         ready.incident_id)], ["reserved","assigned","ready_for_parent"])


if __name__ == "__main__":
    unittest.main()
