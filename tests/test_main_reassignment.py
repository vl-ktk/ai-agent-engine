"""7.4 offline tests: exact owner approval, stop/fence, persistence and integrity."""

from dataclasses import replace
from pathlib import Path
import os
import signal
import subprocess
import sys
from threading import Event, Thread
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.main_plan import decompose_for_review, approve_plan
from ai_agent_engine.main_specialists import (
    approve_catalog, approve_team, assign_specialist, draft_catalog, draft_team,
    parse_candidate, TrustedStepRequirements,
)
from ai_agent_engine.main_reassignment import (
    CheckpointFile, EffectClearance, PriorityWaitPolicy, WaitTier,
    TransferDenied, activate_transfer, approve_transfer, draft_transfer,
    observe_unavailable, owner_transfer_card, transfer_artifacts, transfer_history,
)
from ai_agent_engine.task_api import OwnerScope, TaskInbox, TaskSubmission, TaskNotFound
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate, PermissionDenied
from ai_agent_engine.task_control import TaskProcessController, TaskControlError
from ai_agent_engine.task_state import TaskState, TransitionRejected
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable

from test_main_specialists import (OWNER, FOREIGN, WRITE, EXECUTE, OnePlanModel,
                                   catalog, decision, encoded, role, team)


class ReassignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-74-")
        base = Path(self.temp.name)
        self.db = base / "inbox.sqlite3"
        self.workspaces = base / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        for name in ("slot-one", "slot-two"):
            (self.workspaces / name).mkdir(mode=0o700)
        self.restart(first=True)
        c = draft_catalog(OWNER, catalog(role()))
        self.cat = approve_catalog(self.inbox, OWNER, c, decision(c))
        t = draft_team(OWNER, team(c), self.cat, team_id="team_engine")
        self.team = approve_team(self.inbox, OWNER, t, decision(t))
        p = decompose_for_review(OnePlanModel(), OWNER,
                                 TaskSubmission("engine", "Build", "UI")).proposal
        self.plan = approve_plan(self.inbox, OWNER, p, decision(p))
        self.task_id = self.inbox.accept(TaskSubmission("engine", "Build", "UI"), OWNER).task_id
        self.requirements = TrustedStepRequirements(self.task_id,
                              self.plan.proposal.plan_id, "ui", ("frontend",), (EXECUTE,))
        self.original = parse_candidate(encoded({"agent_id":"ui_one",
                                "provider_id":"gpt", "fallback_reason":None}))
        self.target = parse_candidate(encoded({"agent_id":"ui_two",
                                "provider_id":"claude", "fallback_reason":"unavailable"}))
        assign_specialist(self.inbox, OWNER, self.requirements, "team_engine",
                          self.original, grants=(self.grant("ui_one", EXECUTE),))
        self.checkpoint = (CheckpointFile("output/verified.txt", b"tested", "e"*64),
                           CheckpointFile("output/draft.txt", b"untested"))

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def restart(self, *, first=False):
        if not first:
            self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db),
                               workspace_root=WorkspaceRoot(self.workspaces))

    def grant(self, agent="ui_two", action=EXECUTE, *, expired=False):
        return ActionGrant(self.task_id, agent, action,
                           time.time() - 30 if expired else None)

    def propose(self, *, ttl=900):
        return draft_transfer(self.inbox, OWNER, self.requirements,
                              self.target, self.checkpoint, ttl_seconds=ttl)

    def approve(self, p=None, *, clearance=None):
        p = p or self.propose()
        return approve_transfer(self.inbox, OWNER, p, decision(p),
                                grants=(self.grant(),), clearance=clearance)

    def test_owner_approved_transfer_durable_checkpoint_and_fencing(self):
        p = self.propose()
        attempt = self.approve(p)
        self.assertEqual(attempt.state,"waiting")
        self.assertEqual(attempt.ordinal,1)
        self.assertEqual(len(transfer_history(self.inbox,OWNER,self.task_id)),2)
        self.assertEqual([a.state for a in transfer_history(self.inbox,OWNER,self.task_id)],
                         ["retired","waiting"])
        self.assertEqual(transfer_artifacts(self.inbox,OWNER,attempt.attempt_id),
                         tuple(sorted(self.checkpoint,key=lambda f:f.path)))
        self.assertEqual(self.inbox.state(self.task_id,OWNER),TaskState.ASSIGNED)
        self.assertEqual(self.inbox._assignments[self.task_id].worker_id,"ui_one")
        gate = TaskPermissionGate(self.inbox,grants=(self.grant("ui_one",EXECUTE),))
        with self.assertRaises(PermissionDenied):
            gate.run(self.task_id,OWNER,actor_id="ui_one",action=EXECUTE,
                     operation=lambda: self.fail("old worker escaped"))
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT reason_code FROM task_permission_audit WHERE task_id=? "
            "ORDER BY event_id DESC LIMIT 1",(self.task_id,)).fetchone()[0],
            "transfer_reassigned")
        with self.assertRaises(TransitionRejected):
            self.inbox.transition_state(self.task_id,TaskState.RUNNING,OWNER,
                                        actor_id="ui_one")
        self.restart()
        self.assertEqual(transfer_history(self.inbox,OWNER,self.task_id)[-1],attempt)
        ready = activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                                  available=True,grants=(self.grant(),))
        self.assertEqual(ready.state,"ready")
        with self.assertRaises(TransferDenied):
            activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                              available=True,grants=(self.grant(),))

    def test_denies_wrong_consent_and_changed_proposal(self):
        p = self.propose()
        invalid = [replace(decision(p),owner_id="stranger"),
                   replace(decision(p),proposal_sha256="0"*64),
                   replace(decision(p),channel="bot")]
        for d in invalid:
            with self.subTest(d=d), self.assertRaises(TransferDenied):
                approve_transfer(self.inbox,OWNER,p,d,grants=(self.grant(),))
        with self.assertRaises(TransferDenied):
            self.approve(replace(p,checkpoint=()))
        with self.assertRaises(TransferDenied):
            self.approve(replace(p,target_provider="gpt"))
        with self.assertRaises(TransferDenied):
            approve_transfer(self.inbox,FOREIGN,p,decision(p),grants=(self.grant(),))
        self.assertEqual(transfer_history(self.inbox,OWNER,self.task_id),())

    def test_expired_consent_and_stale_or_replayed_proposal(self):
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1000):
            p = self.propose(ttl=1)
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1002):
            with self.assertRaises(TransferDenied):
                self.approve(p)
        p1 = self.propose()
        p2 = self.propose()
        self.approve(p1)
        for p in (p1,p2):
            with self.assertRaises(TransferDenied):
                self.approve(p)

    def test_missing_or_expired_grant_and_emergency_hold(self):
        p = self.propose()
        for grants in ((),(self.grant("ui_one"),),(self.grant(expired=True),)):
            with self.subTest(grants=grants),self.assertRaises(TransferDenied):
                approve_transfer(self.inbox,OWNER,p,decision(p),grants=grants)
        self.inbox._store._db().execute("""INSERT INTO project_emergency_holds
            (owner_id,project_id,active) VALUES ('vlady','engine',1)""")
        with self.assertRaises(TransferDenied):
            self.approve(p)
        self.inbox._store._db().execute("DELETE FROM project_emergency_holds")
        self.inbox._store._db().commit()
        attempt = self.approve(p)
        with self.assertRaises(TransferDenied):
            activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                              available=True,grants=())
        with self.assertRaises(TransferDenied):
            activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                              available=False,grants=(self.grant(),))

    def test_source_process_must_be_reconciled_before_approval(self):
        self.inbox.transition_state(self.task_id,TaskState.RUNNING,OWNER,
                                    actor_id="ui_one")
        p = self.propose()
        with self.assertRaisesRegex(TransferDenied,"process_untracked"):
            self.approve(p)
        self.assertEqual(transfer_history(self.inbox,OWNER,self.task_id),())

    def test_failure_requires_external_effect_clearance_and_preserves_terminal(self):
        self.inbox.transition_state(self.task_id,TaskState.RUNNING,OWNER,
                                    actor_id="ui_one")
        self.inbox.transition_state(self.task_id,TaskState.FAILED,OWNER,
                                    actor_id="ui_one")
        p = self.propose()
        with self.assertRaisesRegex(TransferDenied,"external_effect_not_reconciled"):
            self.approve(p)
        attempt = self.approve(p,clearance=EffectClearance(
                           self.task_id,"legacy","a"*64))
        self.assertEqual(self.inbox.state(self.task_id,OWNER),TaskState.FAILED)
        self.assertEqual(attempt.ordinal,1)

    def test_terminal_success_never_reopens(self):
        self.inbox.transition_state(self.task_id,TaskState.RUNNING,OWNER,
                                    actor_id="ui_one")
        self.inbox.transition_state(self.task_id,TaskState.SUCCEEDED,OWNER,
                                    actor_id="ui_one")
        with self.assertRaises(TransferDenied):
            self.propose()

    def test_unverified_artifacts_stay_unverified_and_corruption_detected(self):
        attempt = self.approve()
        db = self.inbox._store._db()
        db.execute("""UPDATE task_transfer_artifacts SET body=?
            WHERE attempt_id=? AND path=?""",(b"corrupted",attempt.attempt_id,
                                           "output/verified.txt"))
        with self.assertRaises(StorageUnavailable):
            transfer_artifacts(self.inbox,OWNER,attempt.attempt_id)

    def test_out_of_scope_artifacts_and_history_are_closed(self):
        attempt = self.approve()
        for action in (lambda:transfer_history(self.inbox,FOREIGN,self.task_id),
                       lambda:transfer_artifacts(self.inbox,FOREIGN,attempt.attempt_id)):
            with self.assertRaises((PermissionError,TaskNotFound)):
                action()

    def test_checkpoint_path_and_size_are_bounded(self):
        for path in ("../secret","/tmp/file",".","a//b","a\\b"):
            with self.subTest(path=path),self.assertRaises(ValueError):
                CheckpointFile(path,b"x")
        with self.assertRaises(ValueError):
            CheckpointFile("large",b"a"*2_097_153)

    def test_same_team_alternative_still_needs_fresh_owner_approval(self):
        original = self.approve()
        p = self.propose()
        with self.assertRaises(TransferDenied):
            self.approve(replace(p,target_agent="ui_one"))
        second = self.approve(p)
        self.assertEqual(second.ordinal,2)
        self.assertEqual([a.state for a in transfer_history(self.inbox,OWNER,self.task_id)],
                         ["retired","superseded","waiting"])
        self.assertNotEqual(original.attempt_id,second.attempt_id)

    def test_v13_migration_retains_data_and_no_forged_transfers(self):
        self.inbox.close()
        import sqlite3
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA user_version=13")
            db.execute("DROP TABLE task_transfer_artifacts")
            db.execute("DROP TABLE task_transfer_current")
            db.execute("DROP TABLE task_transfer_attempts")
            db.execute("DROP TABLE task_transfer_proposals")
            db.execute("DROP TABLE task_transfer_audit")
        self.restart(first=True)
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone()[0],16)
        self.assertEqual(self.inbox.state(self.task_id,OWNER),TaskState.ASSIGNED)
        self.assertEqual(transfer_history(self.inbox,OWNER,self.task_id),())

    def test_priority_wait_bound_and_unavailable_poll_are_durable(self):
        policy = PriorityWaitPolicy("vlady","engine",1,
                   (WaitTier(0,30,2),WaitTier(80,5,1)))
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1000):
            attempt = self.approve(self.propose(ttl=90))
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1001):
            first = observe_unavailable(self.inbox,OWNER,attempt.attempt_id,
                                        policy,grants=(self.grant(),))
        self.assertEqual((first.action,first.checks),("wait",1))
        self.restart()
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1002):
            second = observe_unavailable(self.inbox,OWNER,attempt.attempt_id,
                                         policy,grants=(self.grant(),))
        self.assertEqual((second.action,second.checks),
                         ("suggest_alternative",2))
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1003):
            third = observe_unavailable(self.inbox,OWNER,attempt.attempt_id,
                                         policy,grants=(self.grant(),))
        self.assertEqual((third.action,third.checks),
                         ("suggest_alternative",2))
        self.assertEqual(transfer_history(self.inbox,OWNER,self.task_id)[-1].state,
                         "waiting")

    def test_no_fabricated_eta_and_high_priority_poll(self):
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1000):
            attempt = self.approve(self.propose(ttl=90))
        policy = PriorityWaitPolicy("vlady","engine",2,
                   (WaitTier(0,60,5),WaitTier(80,5,1)))
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1001):
            advice = observe_unavailable(self.inbox,OWNER,attempt.attempt_id,
                 policy,grants=(self.grant(),),eta_seconds=100)
        self.assertEqual(advice.action,"suggest_alternative")
        self.assertEqual(transfer_history(self.inbox,OWNER,self.task_id)[-1].state,
                         "waiting")

    def test_wait_after_expired_owner_approval_is_denied(self):
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1000):
            attempt = self.approve(self.propose(ttl=5))
        policy = PriorityWaitPolicy("vlady","engine",1,(WaitTier(0,10,5),))
        with patch("ai_agent_engine.main_reassignment.time.time",return_value=1007):
            with self.assertRaises(TransferDenied):
                observe_unavailable(self.inbox,OWNER,attempt.attempt_id,
                                    policy,grants=(self.grant(),))
            with self.assertRaises(TransferDenied):
                activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                                  available=True,grants=(self.grant(),))

    def test_revoked_grant_and_corrupt_artifact_prevent_activation(self):
        attempt = self.approve()
        db = self.inbox._store._db()
        db.execute("""INSERT INTO task_permission_revocations
            (task_id,worker_id,action) VALUES (?,?,?)""",
            (self.task_id,"ui_two",EXECUTE.value))
        db.commit()
        with self.assertRaises(TransferDenied):
            activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                              available=True,grants=(self.grant(),))
        db.execute("DELETE FROM task_permission_revocations")
        db.execute("""UPDATE task_transfer_artifacts SET body=?
            WHERE attempt_id=? AND path=?""",(b"corrupt",attempt.attempt_id,
                                             "output/draft.txt"))
        db.commit()
        with self.assertRaises(StorageUnavailable):
            activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                              available=True,grants=(self.grant(),))

    def test_managed_fake_worker_must_stop_and_clear_effects(self):
        process = subprocess.Popen([sys.executable,"-c",
                     "import time; time.sleep(30)"],start_new_session=True)
        controller = TaskProcessController(self.inbox)
        try:
            controller.attach(self.task_id,process,OWNER,actor_id="ui_one")
            proposal = self.propose()
            with self.assertRaisesRegex(TransferDenied,"process_not_stopped"):
                self.approve(proposal)
            controller.cancel(self.task_id,OWNER)
            self.assertIsNotNone(process.poll())
            with self.assertRaises(TransferDenied):
                self.approve(proposal)
            proposal = self.propose()  # old RUNNING-state approval is stale
            attempt = self.approve(proposal,clearance=EffectClearance(
                                   self.task_id,"legacy","a"*64))
            self.assertEqual(attempt.state,"waiting")
            self.assertEqual(self.inbox.state(self.task_id,OWNER),TaskState.CANCELLED)
        finally:
            controller.close()
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait(timeout=2)

    def test_legacy_process_attach_rejected_after_transfer(self):
        self.approve()
        process = subprocess.Popen([sys.executable,"-c",
                     "import time; time.sleep(30)"],start_new_session=True)
        controller = TaskProcessController(self.inbox)
        try:
            with self.assertRaises(TaskControlError):
                controller.attach(self.task_id,process,OWNER,actor_id="ui_one")
        finally:
            os.killpg(process.pid,signal.SIGKILL)
            process.wait(timeout=2)
            controller.close()

    def test_owner_card_is_brief_and_exact_version(self):
        proposal = self.propose()
        brief = owner_transfer_card(proposal)
        details = owner_transfer_card(proposal,expanded=True)
        self.assertEqual(brief["approval_sha256"],proposal.sha256)
        self.assertEqual((brief["source_agent"],brief["target_agent"]),
                         ("ui_one","ui_two"))
        self.assertEqual(brief["reason"],"manual_transfer")
        self.assertEqual((brief["verified_files"],brief["unverified_files"]),(1,1))
        self.assertIn("unverified_checkpoint",brief["risks"])
        self.assertNotIn("checkpoint_manifest",brief)
        self.assertEqual(len(details["checkpoint_manifest"]),2)
        self.assertNotIn("untested",str(details))

    def test_priority_change_invalidates_approval_before_and_after(self):
        proposed = self.propose()
        self.inbox.set_priority(self.task_id,90,OWNER)
        with self.assertRaises(TransferDenied):
            self.approve(proposed)
        replacement = self.propose()
        attempt = self.approve(replacement)
        self.inbox.set_priority(self.task_id,20,OWNER)
        with self.assertRaises(TransferDenied):
            activate_transfer(self.inbox,OWNER,attempt.attempt_id,
                              available=True,grants=(self.grant(),))

    def test_transfer_cannot_race_inflight_sensitive_callback(self):
        self.inbox.transition_state(self.task_id,TaskState.RUNNING,OWNER,
                                    actor_id="ui_one")
        store = self.inbox._store
        store.register_process(self.task_id,"ui_one",pid=999999,pgid=999999,
                               sid=999999,start_ticks=1,boot_id="fixture",
                               launch_kind="direct")
        store.mark_process_stopped(self.task_id,999999,1,"fixture")
        proposal = self.propose()
        entered, release, finished = Event(), Event(), Event()
        approved, errors = [], []
        gate = TaskPermissionGate(self.inbox,grants=(self.grant("ui_one"),))

        def callback():
            entered.set()
            if not release.wait(2):
                errors.append("callback timeout")

        def run_callback():
            try:
                gate.run(self.task_id,OWNER,actor_id="ui_one",action=EXECUTE,
                         operation=callback)
            except Exception as exc:
                errors.append(str(exc))

        def run_approval():
            try:
                approved.append(self.approve(proposal))
            except Exception as exc:
                errors.append(str(exc))
            finally:
                finished.set()

        with patch("ai_agent_engine.main_reassignment.group_has_live_members",
                   return_value=False):
            worker = Thread(target=run_callback)
            worker.start()
            self.assertTrue(entered.wait(2))
            transfer = Thread(target=run_approval)
            transfer.start()
            self.assertFalse(finished.wait(.03))
            release.set()
            worker.join(2)
            transfer.join(2)
        self.assertFalse(errors)
        self.assertEqual(len(approved),1)
        self.assertEqual(approved[0].state,"waiting")


if __name__ == "__main__":
    unittest.main()
