"""7.9: exact owner appointments/removals on disposable private SQLite."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_agent_engine.main_global_leads import (
    GlobalLeadDenied, approve_global_lead, draft_global_lead,
    draft_global_lead_removal, global_lead_audit, load_global_lead,
)
from ai_agent_engine.main_orchestrator import OwnerDecision
from ai_agent_engine.main_specialists import approve_catalog, draft_catalog
from ai_agent_engine.task_api import OwnerScope, TaskInbox
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable
from tests.test_main_specialists import catalog, role


OWNER = OwnerScope("vlady",frozenset({"engine","other"}))
OTHER = OwnerScope("other_owner",frozenset({"engine"}))


def owner_decision(proposal, channel="message"):
    return OwnerDecision(proposal.owner_id,proposal.proposal_id,
                         proposal.sha256,channel)


class GlobalLeadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-79-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/"tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db))
        self.addCleanup(lambda: self.inbox.close())
        self.approve_catalog()

    def restart(self):
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db))

    def approve_catalog(self, *, revision=1, roles=None):
        spec = catalog(*(roles if roles is not None else
                         (role("backend"),role("qa",skills=["qa"]))))
        p = draft_catalog(OWNER,spec,revision=revision)
        return approve_catalog(self.inbox,OWNER,p,owner_decision(p))

    def appoint(self, direction="backend", agent="gpt_backend",
                role_id="backend"):
        p = draft_global_lead(self.inbox,OWNER,direction_id=direction,
                              agent_id=agent,role_id=role_id)
        return p,approve_global_lead(self.inbox,OWNER,p,owner_decision(p))

    def test_owner_appoint_replace_remove_persistent_and_audited(self):
        self.assertIsNone(load_global_lead(self.inbox,OWNER,direction_id="backend"))
        original,a = self.appoint()
        self.assertEqual((a.agent_id,a.role_id,a.revision),
                         ("gpt_backend","backend",1))
        self.assertEqual(approve_global_lead(self.inbox,OWNER,original,
                                             owner_decision(original)),a)
        self.restart()
        self.assertEqual(load_global_lead(self.inbox,OWNER,direction_id="backend"),a)
        replacement,b = self.appoint(agent="claude_backend")
        self.assertEqual(b.revision,2)
        self.assertEqual(b.agent_id,"claude_backend")
        remove = draft_global_lead_removal(self.inbox,OWNER,direction_id="backend")
        self.assertIsNone(approve_global_lead(self.inbox,OWNER,remove,
                                              owner_decision(remove,"button")))
        self.assertIsNone(load_global_lead(self.inbox,OWNER,direction_id="backend"))
        self.assertIsNone(approve_global_lead(self.inbox,OWNER,remove,
                                              owner_decision(remove,"button")))
        self.restart()
        self.assertEqual([x[:2] for x in global_lead_audit(
            self.inbox,OWNER,direction_id="backend")],
            [(1,"appoint"),(2,"appoint"),(3,"remove")])
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT count(*) FROM global_direction_decisions").fetchone(),(3,))
        with self.assertRaises(GlobalLeadDenied):
            approve_global_lead(self.inbox,OWNER,replacement,
                                owner_decision(replacement))
        with self.assertRaises(GlobalLeadDenied):
            draft_global_lead_removal(self.inbox,OWNER,direction_id="backend")
        _,c = self.appoint(agent="codex_backend")
        self.assertEqual(c.revision,4)

    def test_independent_directions_and_no_auto_project_privilege(self):
        _,backend=self.appoint()
        _,qa=self.appoint("qa","claude_qa","qa")
        self.assertNotEqual(backend.agent_id,qa.agent_id)
        self.assertIsNone(load_global_lead(self.inbox,OTHER,direction_id="backend"))
        # Nothing adds a project team, task assignment, grant or workspace.
        db=self.inbox._store._db()
        self.assertEqual(db.execute(
            "SELECT count(*) FROM approved_specialist_teams").fetchone(),(0,))
        self.assertEqual(db.execute("SELECT count(*) FROM task_claims").fetchone(),(0,))
        self.assertEqual(db.execute("SELECT count(*) FROM task_permission_revocations")
                         .fetchone(),(0,))
        self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone(),(0,))

    def test_exact_owner_approval_required_cannot_forge_proposal(self):
        proposal=draft_global_lead(self.inbox,OWNER,direction_id="backend",
                                   agent_id="gpt_backend",role_id="backend")
        for decision in (
            OwnerDecision("other_owner",proposal.proposal_id,proposal.sha256,"message"),
            OwnerDecision(OWNER.owner_id,"proposal_"+"a"*32,proposal.sha256,"message"),
            OwnerDecision(OWNER.owner_id,proposal.proposal_id,"0"*64,"button"),
        ):
            with self.assertRaises(GlobalLeadDenied):
                approve_global_lead(self.inbox,OWNER,proposal,decision)
        with self.assertRaises(GlobalLeadDenied):
            approve_global_lead(self.inbox,OTHER,proposal,owner_decision(proposal))
        with self.assertRaises(GlobalLeadDenied):
            approve_global_lead(self.inbox,OWNER,replace(proposal,agent_id="attacker"),
                                owner_decision(proposal))
        self.assertEqual(global_lead_audit(self.inbox,OWNER,
                                           direction_id="backend"),())

    def test_reject_unsupported_role_invalid_worker_and_stale_approval(self):
        with self.assertRaises(GlobalLeadDenied):
            draft_global_lead(self.inbox,OWNER,direction_id="backend",
                              agent_id="gpt_backend",role_id="not_approved")
        with self.assertRaises(ValueError):
            draft_global_lead(self.inbox,OWNER,direction_id="backend",
                              agent_id="../bad",role_id="backend")
        stale=draft_global_lead(self.inbox,OWNER,direction_id="backend",
                                agent_id="first",role_id="backend")
        self.appoint(agent="second")
        with self.assertRaises(GlobalLeadDenied):
            approve_global_lead(self.inbox,OWNER,stale,owner_decision(stale))
        removal=draft_global_lead_removal(self.inbox,OWNER,direction_id="backend")
        self.appoint(agent="third")
        with self.assertRaises(GlobalLeadDenied):
            approve_global_lead(self.inbox,OWNER,removal,owner_decision(removal))

    def test_catalog_change_invalidates_previous_role_until_reapproved(self):
        self.appoint()
        p=draft_global_lead(self.inbox,OWNER,direction_id="qa",
                            agent_id="qa_one",role_id="qa")
        self.approve_catalog(revision=2)
        with self.assertRaises(GlobalLeadDenied):
            approve_global_lead(self.inbox,OWNER,p,owner_decision(p))
        with self.assertRaises(GlobalLeadDenied):
            load_global_lead(self.inbox,OWNER,direction_id="backend")
        _,next_lead=self.appoint(agent="gpt_backend")
        self.assertEqual(next_lead.revision,2)
        self.assertEqual(load_global_lead(self.inbox,OWNER,
                                          direction_id="backend"),next_lead)

    def test_sqlite_failure_is_atomic_and_chain_tampering_is_detected(self):
        self.appoint()
        p=draft_global_lead_removal(self.inbox,OWNER,direction_id="backend")
        db=self.inbox._store._db()
        db.execute("""CREATE TRIGGER block_lead BEFORE INSERT ON
            global_direction_decisions BEGIN SELECT RAISE(ABORT,'test'); END""")
        with self.assertRaises(StorageUnavailable):
            approve_global_lead(self.inbox,OWNER,p,owner_decision(p))
        self.assertEqual(load_global_lead(self.inbox,OWNER,
                                          direction_id="backend").revision,1)
        db.execute("DROP TRIGGER block_lead")
        approve_global_lead(self.inbox,OWNER,p,owner_decision(p))
        db.execute("UPDATE global_direction_decisions SET agent_id='bad' "
                   "WHERE revision=1")
        with self.assertRaises(StorageUnavailable):
            load_global_lead(self.inbox,OWNER,direction_id="backend")

    def test_v15_to_v16_keeps_existing_task_schema_and_empty_lead_ledger(self):
        self.inbox.close()
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE global_direction_decisions")
            db.execute("PRAGMA user_version=15")
        self.restart()
        db=self.inbox._store._db()
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(),(16,))
        self.assertEqual(db.execute("SELECT count(*) FROM task_crisis_escalations")
                         .fetchone(),(0,))
        self.assertIsNone(load_global_lead(self.inbox,OWNER,direction_id="backend"))
        self.appoint()


if __name__ == "__main__":
    unittest.main()
