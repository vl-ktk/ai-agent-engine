"""7.3: bounded approved specialist matching; offline SQLite and real flock."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.main_plan import decompose_for_review, approve_plan
from ai_agent_engine.main_specialists import (
    InvalidSpecialist, SpecialistDenied, TrustedStepRequirements,
    approve_catalog, approve_team, assign_specialist, draft_catalog, draft_team,
    load_catalog, load_team, load_specialist_pin, parse_catalog,
    parse_candidate, parse_team, run_pinned_action, specialist_audit,
)
from ai_agent_engine.task_api import (OwnerScope, ProjectForbidden, TaskInbox,
                                      TaskNotFound, TaskSubmission)
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_permissions import (ActionGrant, PermissionDenied,
                                              ProtectedAction, TaskPermissionGate)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("vlady",frozenset({"engine","other"}))
FOREIGN = OwnerScope("stranger",frozenset({"engine"}))
WRITE = ProtectedAction.WRITE_WORKSPACE
EXECUTE = ProtectedAction.EXECUTE_TOOL


def encoded(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False).encode()


def role(role_id="frontend", **changes):
    values = dict(role_id=role_id,name="Frontend engineer",
                  instructions="Build only reviewed UI tasks",
                  boundaries="No deployments without an explicit grant",
                  skills=["typescript","frontend"],tools=["git","unit_test"],
                  actions=["write_workspace","execute_tool"],
                  primary_provider="gpt",fallback_providers=["claude"])
    values.update(changes)
    return values


def catalog(*roles):
    return encoded({"schema_version":1,"roles":list(roles)})


def team(catalog_proposal, *, project="engine", agents=None, local_roles=None,
         limits=None):
    agents = agents if agents is not None else [
        {"agent_id":"ui_one","role_id":"frontend","workspace_name":"slot-one",
         "actions":["write_workspace","execute_tool"]},
        {"agent_id":"ui_two","role_id":"frontend","workspace_name":"slot-two",
         "actions":["execute_tool"]},
    ]
    limits = limits if limits is not None else [
        {"role_id":"frontend","max_instances":2}]
    return encoded({"schema_version":1,"project_id":project,
                    "catalog_revision":catalog_proposal.revision,
                    "catalog_sha256":catalog_proposal.sha256,
                    "local_roles":local_roles if local_roles is not None else [],
                    "members":agents,"role_limits":limits})


def step(name="ui"):
    return dict(step_id=name,project_id="engine",title="Build interface",
                goal="Reviewed interface",instructions="Implement safe UI",
                boundaries="No deploy",expected_result="Component and tests",
                acceptance_checks=["Unit tests"],expected_artifacts=["Patch"],
                depends_on=[],parent_step_id=None)


class OnePlanModel:
    def propose(self, source, feedback, detail_hint):
        return encoded({"schema_version":1,"steps":[step()]})

    def review(self, source, plan_json):
        from hashlib import sha256
        return encoded({"plan_sha256":sha256(plan_json).hexdigest(),
                        "passed":True,"summary":"Valid", "findings":[]})


def decision(proposal):
    return OwnerDecision(proposal.owner_id,proposal.proposal_id,
                         proposal.sha256,"button")


class SpecialistContractTests(unittest.TestCase):
    def test_catalog_rejects_duplicate_unknown_and_injected_fields(self):
        for raw in (
            catalog(role(),role()),
            catalog(role(actions=["execute_tool","unknown_action"])),
            catalog(role(primary_provider="gpt",fallback_providers=["gpt"])),
            catalog(role(tools=["git","git"])),
            catalog(role(skills=[])),
            encoded({"schema_version":1,"roles":[dict(role(),owner_id="stranger")]}),
            b'{"schema_version":1,"roles":[],"roles":[]}',
            encoded({"schema_version":True,"roles":[role()]}),
            b'{}',
        ):
            with self.subTest(raw=raw[:50]), self.assertRaises(InvalidSpecialist):
                parse_catalog(raw)

    def test_unknown_candidate_fields_cannot_assign_actions(self):
        for raw in (
            encoded({"agent_id":"ui_one","provider_id":"gpt",
                     "fallback_reason":None,"actions":["read_secret"]}),
            encoded({"agent_id":"ui_one","provider_id":"../../gpt",
                     "fallback_reason":None}),
            b'{"agent_id":"ui_one","agent_id":"ui_two","provider_id":"gpt","fallback_reason":null}',
        ):
            with self.assertRaises(InvalidSpecialist):
                parse_candidate(raw)

    def test_team_rejects_role_capacity_workspace_and_scope_expansion(self):
        scope = OWNER
        cat = draft_catalog(scope,catalog(role()))
        scenarios = [
            dict(agents=[{"agent_id":"bad","role_id":"invented",
                          "workspace_name":"slot-one","actions":[]}],
                 limits=[{"role_id":"invented","max_instances":1}]),
            dict(limits=[{"role_id":"frontend","max_instances":1}]),
            dict(agents=[{"agent_id":"ui_one","role_id":"frontend",
                          "workspace_name":"../escape","actions":[]}],
                 limits=[{"role_id":"frontend","max_instances":1}]),
            dict(agents=[{"agent_id":"ui_one","role_id":"frontend",
                          "workspace_name":"slot-one","actions":["read_secret"]}],
                 limits=[{"role_id":"frontend","max_instances":1}]),
            dict(local_roles=[role()]),
            dict(project="foreign"),
        ]
        for scenario in scenarios:
            with self.subTest(scenario=scenario), self.assertRaises(
                    (InvalidSpecialist,ProjectForbidden)):
                parse_team(team(cat,**scenario),scope,cat.spec,
                           cat.revision,cat.sha256)


class SpecialistSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-73-")
        base = Path(self.temp.name)
        self.workspaces = base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        for name in ("slot-one","slot-two","slot-three"):
            (self.workspaces / name).mkdir(mode=0o700)
        self.db = base / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                               workspace_root=WorkspaceRoot(self.workspaces))
        self.approved_cat = self.new_catalog()
        self.approved_team = self.new_team()
        self.plan = self.new_plan()

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def restart(self):
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                               workspace_root=WorkspaceRoot(self.workspaces))

    def new_catalog(self, roles=None, revision=1):
        proposal = draft_catalog(OWNER,catalog(*(roles if roles is not None else [role()])),
                                 revision=revision)
        return approve_catalog(self.inbox,OWNER,proposal,decision(proposal))

    def new_team(self, cat=None, revision=1, agents=None, local_roles=None,
                 limits=None):
        cat = cat or self.approved_cat
        proposed = draft_team(OWNER,team(cat.proposal,agents=agents,
                                        local_roles=local_roles,limits=limits),cat,
                              team_id="team_engine",revision=revision)
        return approve_team(self.inbox,OWNER,proposed,decision(proposed))

    def new_plan(self):
        source = TaskSubmission("engine","Build site","Plan and review")
        proposed = decompose_for_review(OnePlanModel(),OWNER,source).proposal
        return approve_plan(self.inbox,OWNER,proposed,decision(proposed))

    def task(self, project="engine"):
        return self.inbox.accept(TaskSubmission(project,"Task","Private"),OWNER).task_id

    def requirements(self, task_id, *, skills=("frontend",), actions=(WRITE,)):
        return TrustedStepRequirements(task_id,self.plan.proposal.plan_id,
                                       "ui",skills,actions)

    def candidate(self, agent="ui_one",provider="gpt",reason=None):
        return parse_candidate(encoded({"agent_id":agent,"provider_id":provider,
                                        "fallback_reason":reason}))

    def grant(self, task_id, worker="ui_one", action=WRITE, expires_at=None):
        return ActionGrant(task_id,worker,action,expires_at)

    def assign(self,task_id, *, candidate=None, req=None, grants=None):
        return assign_specialist(self.inbox,OWNER,req or self.requirements(task_id),
                                 "team_engine",candidate or self.candidate(),
                                 grants=grants if grants is not None
                                 else (self.grant(task_id),))

    def test_catalog_team_approval_exact_replay_no_activation(self):
        self.assertEqual(load_catalog(self.inbox,OWNER),self.approved_cat)
        self.assertEqual(load_team(self.inbox,OWNER,"team_engine"),self.approved_team)
        self.assertEqual(approve_team(self.inbox,OWNER,self.approved_team.proposal,
                                      decision(self.approved_team.proposal)),self.approved_team)
        self.assertEqual(self.inbox.snapshot(OWNER),())
        self.assertIsNone(load_catalog(self.inbox,FOREIGN))
        self.assertIsNone(load_team(self.inbox,FOREIGN,"team_engine"))
        self.restart()
        self.assertEqual(load_catalog(self.inbox,OWNER),self.approved_cat)
        self.assertEqual(load_team(self.inbox,OWNER,"team_engine"),self.approved_team)

    def test_tampered_consent_cannot_register_profiles_or_teams(self):
        proposal = draft_catalog(OWNER,catalog(role("backend")),revision=2)
        with self.assertRaises(InvalidSpecialist):
            approve_catalog(self.inbox,OWNER,proposal,replace(decision(proposal),
                                                              proposal_sha256="0"*64))
        with self.assertRaises(InvalidSpecialist):
            approve_catalog(self.inbox,OWNER,replace(proposal,sha256="0"*64),
                            replace(decision(proposal),proposal_sha256="0"*64))
        self.assertEqual(load_catalog(self.inbox,OWNER).proposal.revision,1)
        with self.assertRaises(InvalidSpecialist):
            approve_team(self.inbox,OWNER,self.approved_team.proposal,
                         replace(decision(self.approved_team.proposal),owner_id="stranger"))

    def test_revision_cas_replay_and_stale_team(self):
        newcat = self.new_catalog([role(),role("backend",skills=["backend"])],2)
        with self.assertRaises(StorageUnavailable):
            self.new_catalog([role()],2)
        with self.assertRaises(StorageUnavailable):
            self.new_catalog([role()],4)
        self.assertEqual(load_catalog(self.inbox,OWNER,1),self.approved_cat)
        self.assertEqual(load_catalog(self.inbox,OWNER).proposal.revision,2)
        self.new_team(newcat,2)
        self.assertEqual(load_team(self.inbox,OWNER,"team_engine").proposal.revision,2)

    def test_owner_can_approve_local_role_with_team_one_click(self):
        agent = [{"agent_id":"instagram_one","role_id":"instagram",
                  "workspace_name":"slot-three","actions":["execute_tool"]}]
        profiles=[role("instagram",skills=["instagram"],actions=["execute_tool"])]
        limits=[{"role_id":"instagram","max_instances":1}]
        approved = self.new_team(revision=2,agents=agent,
                                 local_roles=profiles,limits=limits)
        self.assertEqual(approved.proposal.spec.local_roles[0].role_id,"instagram")

    def test_valid_matching_pins_exact_versions_and_audits(self):
        task_id = self.task()
        pin = self.assign(task_id)
        self.assertEqual(pin.role_id,"frontend")
        self.assertEqual(pin.agent_id,"ui_one")
        self.assertEqual(pin.team_digest,self.approved_team.proposal.sha256)
        self.assertEqual(pin.catalog_digest,self.approved_cat.proposal.sha256)
        self.assertEqual(self.inbox.state(task_id,OWNER),TaskState.ASSIGNED)
        self.assertEqual(load_specialist_pin(self.inbox,OWNER,task_id),pin)
        self.assertEqual(specialist_audit(self.inbox,OWNER,task_id)[0][3],"assigned")
        self.restart()
        self.assertEqual(load_specialist_pin(self.inbox,OWNER,task_id),pin)

    def test_missing_grant_and_expired_grant_denied_without_claim(self):
        for grants in ((),(self.grant(self.task()),)):
            task_id=self.task()
            with self.assertRaises(SpecialistDenied):
                self.assign(task_id,grants=grants)
            self.assertIsNone(self.inbox.get_assignment(task_id,OWNER))
            self.assertEqual(specialist_audit(self.inbox,OWNER,task_id)[0][3],
                             "grant_missing_or_revoked")
        task_id = self.task()
        with self.assertRaises(SpecialistDenied):
            self.assign(task_id,grants=(self.grant(task_id,expires_at=1),))

    def test_unauthorized_skill_role_action_provider_are_refused(self):
        cases=[
            dict(req_skills=("security",),actions=(WRITE,)),
            dict(req_skills=("frontend",),actions=(ProtectedAction.READ_SECRET,)),
        ]
        for values in cases:
            task_id=self.task()
            req=self.requirements(task_id,skills=values["req_skills"],
                                  actions=values["actions"])
            with self.assertRaises(SpecialistDenied):
                self.assign(task_id,req=req,grants=(self.grant(task_id,
                            action=values["actions"][0]),))
            self.assertIsNone(self.inbox.get_assignment(task_id,OWNER))
        for candidate in (self.candidate("stranger"),self.candidate(provider="codex"),
                          self.candidate(provider="claude"),
                          self.candidate(reason="capacity")):
            task_id=self.task()
            with self.assertRaises(SpecialistDenied):
                self.assign(task_id,candidate=candidate)
            self.assertIsNone(load_specialist_pin(self.inbox,OWNER,task_id))

    def test_alternative_provider_requires_explanation(self):
        task_id = self.task()
        pin = self.assign(task_id,candidate=self.candidate(provider="claude",
                                                            reason="capacity"))
        self.assertEqual(pin.provider_id,"claude")
        self.assertEqual(pin.provider_reason,"capacity")
        self.restart()
        self.assertEqual(load_specialist_pin(self.inbox,OWNER,task_id).provider_reason,
                         "capacity")

    def test_other_project_same_owner_is_not_write_authority(self):
        task_id = self.task("other")
        with self.assertRaises(SpecialistDenied):
            self.assign(task_id)
        self.assertIsNone(load_specialist_pin(self.inbox,OWNER,task_id))
        with self.assertRaises(TaskNotFound):
            assign_specialist(self.inbox,FOREIGN,self.requirements(task_id),
                              "team_engine",self.candidate(),
                              grants=(self.grant(task_id),))

    def test_updated_team_does_not_rewrite_old_task_pin(self):
        task_id = self.task()
        pin = self.assign(task_id)
        new_cat = self.new_catalog([role(),role("backend",skills=["backend"])],2)
        self.new_team(new_cat,2,agents=[{"agent_id":"ui_three",
            "role_id":"frontend","workspace_name":"slot-three",
            "actions":["write_workspace"]}],
            limits=[{"role_id":"frontend","max_instances":1}])
        self.assertEqual(load_specialist_pin(self.inbox,OWNER,task_id),pin)
        task2=self.task()
        pin2=self.assign(task2,candidate=self.candidate("ui_three"),
                         grants=(self.grant(task2,worker="ui_three"),))
        self.assertEqual(pin2.team_revision,2)
        self.assertEqual(pin.team_revision,1)
        self.restart()
        self.assertEqual(load_specialist_pin(self.inbox,OWNER,task_id),pin)

    def test_revocation_and_emergency_hold_block_old_version_action(self):
        task_id=self.task()
        pin=self.assign(task_id)
        self.inbox.transition_state(task_id,TaskState.RUNNING,OWNER,actor_id="ui_one")
        grant=self.grant(task_id)
        gate=TaskPermissionGate(self.inbox,grants=(grant,))
        performed=[]
        run=lambda:run_pinned_action(self.inbox,gate,OWNER,task_id,
                                    actor_id="ui_one",action=WRITE,
                                    operation=lambda:performed.append("done"))
        run()
        self.assertEqual(performed,["done"])
        # New team must not affect the old task's matched profile.
        newcat=self.new_catalog([role(actions=["execute_tool"])],2)
        self.new_team(newcat,2,agents=[{"agent_id":"ui_two",
                         "role_id":"frontend","workspace_name":"slot-two",
                         "actions":["execute_tool"]}],
                      limits=[{"role_id":"frontend","max_instances":1}])
        self.assertEqual(load_specialist_pin(self.inbox,OWNER,task_id),pin)
        gate.set_emergency_hold(OWNER,"engine",active=True)
        with self.assertRaises(PermissionDenied):
            run()
        gate.set_emergency_hold(OWNER,"engine",active=False)
        gate.revoke(OWNER,grant)
        with self.assertRaises(PermissionDenied):
            run()
        self.assertEqual(performed,["done"])
        self.assertEqual([x.reason_code for x in gate.audit_history(task_id,OWNER)],
                         ["granted","emergency_hold","grant_revoked"])
        self.restart()
        gate=TaskPermissionGate(self.inbox,grants=(grant,))
        with self.assertRaises(PermissionDenied):
            run_pinned_action(self.inbox,gate,OWNER,task_id,actor_id="ui_one",
                              action=WRITE,operation=lambda:performed.append("bad"))

    def test_no_cross_role_ability_even_with_broad_gate_grant(self):
        task_id=self.task()
        self.assign(task_id)
        self.inbox.transition_state(task_id,TaskState.RUNNING,OWNER,actor_id="ui_one")
        gate=TaskPermissionGate(self.inbox,grants=(self.grant(task_id),
                self.grant(task_id,action=ProtectedAction.DELETE_ARTIFACT)))
        performed=[]
        with self.assertRaises(PermissionDenied):
            run_pinned_action(self.inbox,gate,OWNER,task_id,actor_id="ui_one",
                              action=ProtectedAction.DELETE_ARTIFACT,
                              operation=lambda:performed.append("bad"))
        self.assertFalse(performed)
        # Even bypassing the optional pin wrapper does NOT bypass the
        # base TaskPermissionGate's persisted specialist action ceiling.
        with self.assertRaises(PermissionDenied):
            gate.run(task_id,OWNER,actor_id="ui_one",
                     action=ProtectedAction.DELETE_ARTIFACT,
                     operation=lambda:performed.append("bad"))
        self.assertEqual(gate.audit_history(task_id,OWNER)[-1].reason_code,
                         "role_policy_denied")

    def test_revoked_grant_and_project_hold_prevent_initial_match(self):
        task_id=self.task()
        grant=self.grant(task_id)
        gate=TaskPermissionGate(self.inbox,grants=(grant,))
        gate.revoke(OWNER,grant)
        with self.assertRaises(SpecialistDenied):
            self.assign(task_id,grants=(grant,))
        self.assertEqual(self.inbox.state(task_id,OWNER),TaskState.QUEUED)
        # A separately admitted new task is denied during an emergency hold.
        task2=self.task()
        gate.set_emergency_hold(OWNER,"engine",active=True)
        self.restart()
        with self.assertRaises(SpecialistDenied):
            self.assign(task2)
        self.assertIsNone(self.inbox.get_assignment(task2,OWNER))
        TaskPermissionGate(self.inbox).set_emergency_hold(OWNER,"engine",active=False)
        self.assign(task2)

    def test_tampered_pin_cannot_be_reused_for_runtime_action(self):
        task_id=self.task()
        self.assign(task_id)
        self.inbox.transition_state(task_id,TaskState.RUNNING,OWNER,actor_id="ui_one")
        gate=TaskPermissionGate(self.inbox,grants=(self.grant(task_id),))
        with self.inbox._store._db() as db:
            db.execute("""UPDATE task_specialist_pins SET team_digest=?
                          WHERE task_id=?""",("0"*64,task_id))
        ran=[]
        with self.assertRaises(StorageUnavailable):
            run_pinned_action(self.inbox,gate,OWNER,task_id,
                              actor_id="ui_one",action=WRITE,
                              operation=lambda:ran.append("bad"))
        self.assertFalse(ran)

    def test_approval_database_failure_is_atomic(self):
        p=draft_catalog(OWNER,catalog(role(),role("backend")),revision=2)
        db=self.inbox._store._db()
        db.execute("""CREATE TRIGGER deny_catalog BEFORE INSERT ON
                      approved_specialist_catalogs
                      BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
        with self.assertRaises(StorageUnavailable):
            approve_catalog(self.inbox,OWNER,p,decision(p))
        self.assertEqual(load_catalog(self.inbox,OWNER).proposal.revision,1)
        db.execute("DROP TRIGGER deny_catalog")
        approve_catalog(self.inbox,OWNER,p,decision(p))
        self.assertEqual(load_catalog(self.inbox,OWNER).proposal.revision,2)

    def test_claim_atomicity_on_sqlite_failure(self):
        task_id=self.task()
        db=self.inbox._store._db()
        db.execute("""CREATE TRIGGER deny_pin BEFORE INSERT ON task_specialist_pins
                      BEGIN SELECT RAISE(ABORT, 'test denial'); END""")
        with self.assertRaises(StorageUnavailable):
            self.assign(task_id)
        self.assertIsNone(self.inbox.get_assignment(task_id,OWNER))
        self.assertEqual(self.inbox.state(task_id,OWNER),TaskState.QUEUED)
        self.assertIsNone(load_specialist_pin(self.inbox,OWNER,task_id))
        db.execute("DROP TRIGGER deny_pin")
        self.assign(task_id)

    def test_saved_approval_tampering_denied_on_read_and_assignment(self):
        with self.inbox._store._db() as db:
            db.execute("UPDATE approved_specialist_teams SET canonical_json='{}'")
        with self.assertRaises(StorageUnavailable):
            load_team(self.inbox,OWNER,"team_engine")
        with self.assertRaises(StorageUnavailable):
            self.assign(self.task())

    def test_v12_to_v13_migration_preserves_old_plan_and_no_forged_teams(self):
        original_plan=self.plan.proposal.plan_id
        self.inbox.close()
        with sqlite3.connect(self.db) as db:
            for name in ("approved_specialist_catalogs","approved_specialist_teams",
                         "task_specialist_pins","task_specialist_audit",
                         "task_permission_revocations","project_emergency_holds"):
                db.execute("DROP TABLE " + name)
            # Simulate a genuine v12 permission audit layout.
            db.execute("ALTER TABLE task_permission_audit RENAME TO old_audit")
            db.execute("""CREATE TABLE task_permission_audit (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                action TEXT NOT NULL,allowed INTEGER NOT NULL,
                reason_code TEXT NOT NULL,principal_kind TEXT NOT NULL,
                occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("DROP TABLE old_audit")
            db.execute("PRAGMA user_version=12")
        self.inbox=TaskInbox(store=SQLiteTaskStore(self.db),
                             workspace_root=WorkspaceRoot(self.workspaces))
        self.assertEqual(self.inbox._store._db().execute("PRAGMA user_version").fetchone(),
                         (15,))
        self.assertIsNotNone(self.inbox.latest_approved_plan(original_plan,OWNER))
        self.assertIsNone(load_catalog(self.inbox,OWNER))
        self.assertIsNone(load_team(self.inbox,OWNER,"team_engine"))


if __name__ == "__main__":
    unittest.main()
