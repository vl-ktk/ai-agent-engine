"""7.10: offline peer exchange using already approved roles and grants."""

from __future__ import annotations

import sys
import time
import unittest

if sys.platform.startswith("linux"):
    from ai_agent_engine.main_peer_exchange import (
        PeerExchangeDenied, offer_peer_subtask, peer_requirements,
        verified_peer_result,
    )
    from ai_agent_engine.main_specialists import (
        SpecialistDenied, assign_specialist, parse_candidate,
    )
    from ai_agent_engine.task_api import IdempotencyConflict, OwnerScope
    from ai_agent_engine.task_delivery import TaskDeliveryManager
    from ai_agent_engine.task_permissions import (
        ActionGrant, PermissionDenied, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_state import TaskState
    import test_main_escalation_7_8 as crisis_fixture
    from test_main_specialists import encoded

    SCOPE = OwnerScope("owner78", frozenset({"engine"}))
    EXECUTE = ProtectedAction.EXECUTE_TOOL


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private SQLite only")
class PeerExchangeTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real approved 7.3 plan/team/task fixture from 7.8.
        self.fx = crisis_fixture.CrisisEscalationTests(
            "test_success_requires_verified_source_and_independent_qa")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.inbox = self.fx.inbox
        self.parent = self.fx.original

    def offer(self, exchange_id="peer_" + "1" * 32, candidate=None):
        return offer_peer_subtask(
            self.inbox, SCOPE, exchange_id=exchange_id,
            requirements=self.fx.parent_req, team_id="team_engine",
            candidate=candidate or self.fx.candidate())

    def activate(self, receipt):
        req = peer_requirements(self.inbox, SCOPE, receipt.child_task_id)
        return assign_specialist(
            self.inbox, SCOPE, req, "team_engine", self.fx.candidate(),
            grants=(ActionGrant(receipt.child_task_id, "crisis_one", EXECUTE,
                                time.time() + 60),))

    def completed(self, receipt):
        self.activate(receipt)
        delivery = TaskDeliveryManager(self.inbox)
        ticket = delivery.reserve(receipt.child_task_id, SCOPE,
                                  actor_id="crisis_one", max_attempts=1)
        self.inbox.transition_state(receipt.child_task_id, TaskState.RUNNING,
                                    SCOPE, actor_id="crisis_one")
        self.inbox.transition_state(receipt.child_task_id, TaskState.SUCCEEDED,
                                    SCOPE, actor_id="crisis_one")
        delivery.report(receipt.child_task_id, SCOPE, actor_id="crisis_one",
                        token=ticket.token, success=True)
        self.inbox._store.write_source_version(receipt.child_task_id,
                                               "crisis_one", "a" * 40,
                                               "b" * 40, None)

    def test_peer_child_durable_and_original_remains_owner(self):
        exchange = self.offer()
        self.assertEqual(exchange.parent_task_id, self.parent)
        self.assertEqual(exchange.original_agent, "ui_one")
        self.assertEqual(exchange.peer_agent, "crisis_one")
        self.assertEqual(self.offer(), exchange)
        self.assertEqual(self.inbox.get_assignment(self.parent, SCOPE).worker_id,
                         "ui_one")
        self.assertEqual(self.inbox.state(self.parent, SCOPE), TaskState.ASSIGNED)
        self.assertEqual(self.inbox.state(exchange.child_task_id, SCOPE),
                         TaskState.QUEUED)
        self.assertEqual(len(self.inbox.snapshot(SCOPE)), 2)
        self.fx.inbox.close()
        from ai_agent_engine.task_api import TaskInbox
        from ai_agent_engine.task_storage import SQLiteTaskStore
        from ai_agent_engine.task_assignment import WorkspaceRoot
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.fx.db),
                               workspace_root=WorkspaceRoot(self.fx.workspaces))
        self.fx.inbox = self.inbox
        self.assertEqual(self.offer(), exchange)
        self.assertEqual(peer_requirements(
            self.inbox, SCOPE, exchange.child_task_id).task_id,
            exchange.child_task_id)

    def test_scope_provider_team_and_same_agent_fenced(self):
        with self.assertRaises(PeerExchangeDenied):
            self.offer(candidate=self.fx.candidate("ui_one"))
        with self.assertRaises(PeerExchangeDenied):
            self.offer(candidate=self.fx.candidate("crisis_one", "unapproved"))
        with self.assertRaises(PeerExchangeDenied):
            offer_peer_subtask(self.inbox, OwnerScope("other", frozenset({"engine"})),
                exchange_id="peer_" + "2" * 32,
                requirements=self.fx.parent_req, team_id="team_engine",
                candidate=self.fx.candidate())
        self.assertEqual(len(self.inbox.snapshot(SCOPE)), 1)

    def test_peer_requires_its_own_grant_without_inheritance(self):
        receipt = self.offer()
        req = peer_requirements(self.inbox, SCOPE, receipt.child_task_id)
        with self.assertRaises(SpecialistDenied):
            assign_specialist(self.inbox, SCOPE, req, "team_engine",
                              self.fx.candidate(), grants=(ActionGrant(
                                  self.parent, "ui_one", EXECUTE),))
        self.activate(receipt)
        gate = TaskPermissionGate(self.inbox, grants=(ActionGrant(
            self.parent, "ui_one", EXECUTE),))
        with self.assertRaises(PermissionDenied):
            gate.run(receipt.child_task_id, SCOPE, actor_id="crisis_one",
                     action=EXECUTE, operation=lambda: self.fail("grant inherited"))
        self.assertEqual(self.inbox.get_assignment(self.parent, SCOPE).worker_id,
                         "ui_one")
        self.assertEqual(self.inbox.get_assignment(
            receipt.child_task_id, SCOPE).worker_id, "crisis_one")

    def test_unverified_child_never_returned(self):
        receipt = self.offer()
        with self.assertRaises(PeerExchangeDenied):
            verified_peer_result(self.inbox, SCOPE, receipt.child_task_id,
                                 trusted_qa=lambda: True)
        self.completed(receipt)
        with self.assertRaises(PeerExchangeDenied):
            verified_peer_result(self.inbox, SCOPE, receipt.child_task_id,
                                 trusted_qa=lambda: False)
        with self.assertRaises(PeerExchangeDenied):
            verified_peer_result(self.inbox, SCOPE, receipt.child_task_id,
                                 trusted_qa=lambda: 1)
        result = verified_peer_result(self.inbox, SCOPE, receipt.child_task_id,
                                      trusted_qa=lambda: True)
        self.assertEqual(result.exchange, receipt)
        self.assertEqual(result.baseline_commit, "a" * 40)
        self.assertEqual(result.result_commit, "b" * 40)
        self.assertEqual(self.inbox.state(self.parent, SCOPE), TaskState.ASSIGNED)
        self.assertEqual(self.inbox.get_assignment(self.parent, SCOPE).worker_id,
                         "ui_one")

    def test_idempotency_key_cannot_switch_peer_provider(self):
        original = self.offer()
        alternate = parse_candidate(encoded({"agent_id":"crisis_one",
            "provider_id":"claude", "fallback_reason":"approved_fallback"}))
        with self.assertRaises(IdempotencyConflict):
            self.offer(candidate=alternate)
        self.assertEqual(self.offer(), original)
        self.assertEqual(len(self.inbox.snapshot(SCOPE)), 2)


if __name__ == "__main__":
    unittest.main()
