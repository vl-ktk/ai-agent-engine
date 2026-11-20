"""7.11: real subprocess adapters obey the shared admission boundary offline."""

from __future__ import annotations

import sys
import unittest

if sys.platform.startswith("linux"):
    from ai_agent_engine.main_shared_capacity import (
        CapacityPolicy, ProviderBudget, SharedCapacity,
    )
    from ai_agent_engine.task_claude import ClaudeTaskExecutor
    from ai_agent_engine.task_gpt import GPTTaskError, GPTTaskExecutor
    from ai_agent_engine.task_permissions import (
        ActionGrant, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_state import TaskState
    from test_main_specialists import OWNER, SpecialistSQLiteTests, role


ECHO = (
    "import json,sys; "
    "data=json.loads(sys.stdin.buffer.read()); "
    "print(json.dumps({'ok':True,'text':'offline:'+data['prompt']}),flush=True)"
)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private SQLite only")
class PinnedProviderCapacityTests(unittest.TestCase):
    def setUp(self):
        self.fx = SpecialistSQLiteTests(
            "test_catalog_team_approval_exact_replay_no_activation")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.inbox = self.fx.inbox
        self.runners = []
        # One previously approved team, now explicitly permitting real model
        # requests for *these* disposable tasks. The model cannot add grants.
        cat = self.fx.new_catalog([
            role(actions=["write_workspace", "execute_tool", "external_request"])
        ], revision=2)
        self.fx.new_team(cat, revision=2, agents=[
            {"agent_id": "ui_one", "role_id": "frontend",
             "workspace_name": "slot-one", "actions": ["external_request"]},
            {"agent_id": "ui_two", "role_id": "frontend",
             "workspace_name": "slot-two", "actions": ["external_request"]},
        ])
        self.capacity = SharedCapacity(self.inbox, CapacityPolicy(1, (
            ProviderBudget("gpt", 1, 1, 60),
            ProviderBudget("claude", 1, 1, 60),
        )))

    def tearDown(self):
        for runner in self.runners:
            runner.close()

    def assigned(self, worker, provider):
        task_id = self.fx.task()
        action = ProtectedAction.EXTERNAL_REQUEST
        self.fx.assign(
            task_id, candidate=self.fx.candidate(
                worker, provider, "approved_fallback" if provider == "claude" else None),
            req=self.fx.requirements(task_id, actions=(action,)),
            grants=(ActionGrant(task_id, worker, action),))
        return task_id

    def runner(self, worker, provider, *, capacity=True):
        gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(self.current_task, worker, ProtectedAction.EXTERNAL_REQUEST),
        ))
        options = dict(worker_id=worker, permission_gate=gate,
                       worker_argv=(sys.executable, "-u", "-c", ECHO),
                       capacity=self.capacity if capacity else None,
                       timeout_seconds=5)
        if provider == "gpt":
            runner = GPTTaskExecutor(self.inbox, OWNER, model_id="offline", **options)
        else:
            runner = ClaudeTaskExecutor(self.inbox, OWNER,
                authorized_owner_id=OWNER.owner_id, model_id="offline", **options)
        self.runners.append(runner)
        return runner

    def test_pinned_gpt_executes_once_and_settles_cpu_slot(self):
        self.current_task = self.assigned("ui_one", "gpt")
        runner = self.runner("ui_one", "gpt")
        result = runner.execute(self.current_task, "slot-one")
        self.assertEqual((result.state, result.executed),
                         (TaskState.SUCCEEDED, True))
        self.assertEqual(result.text, "offline:Private")
        self.assertIsNone(self.inbox._store.capacity_reservation(self.current_task))
        self.assertFalse(runner.execute(self.current_task, "slot-one").executed)
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT count(*) FROM task_capacity_reservations").fetchone(), (1,))

    def test_cpu_busy_defers_claude_without_process_or_delivery(self):
        first = self.assigned("ui_one", "gpt")
        second = self.assigned("ui_two", "claude")
        admitted = self.capacity.acquire(first, OWNER, worker_id="ui_one")
        self.assertTrue(admitted.admitted)
        self.current_task = second
        runner = self.runner("ui_two", "claude")
        wait = runner.execute(second, "slot-two")
        self.assertEqual((wait.state, wait.executed, wait.capacity_reason),
                         (TaskState.ASSIGNED, False, "cpu_busy"))
        self.assertIsNone(runner.delivery.snapshot(second, OWNER))
        self.assertIsNone(self.inbox._store.process_record(second))
        self.inbox.transition_state(first, TaskState.FAILED, OWNER,
                                    actor_id="ui_one")
        self.assertTrue(self.capacity.release(first, OWNER,
            worker_id="ui_one", reservation_id=admitted.reservation_id))
        result = runner.execute(second, "slot-two")
        self.assertEqual((result.state, result.executed),
                         (TaskState.SUCCEEDED, True))

    def test_quota_defers_without_consuming_delivery(self):
        first = self.assigned("ui_one", "gpt")
        self.current_task = first
        self.assertTrue(self.runner("ui_one", "gpt").execute(first, "slot-one").executed)
        second = self.assigned("ui_two", "gpt")
        self.current_task = second
        runner = self.runner("ui_two", "gpt")
        wait = runner.execute(second, "slot-two")
        self.assertEqual(wait.capacity_reason, "provider_quota")
        self.assertIsNone(runner.delivery.snapshot(second, OWNER))

    def test_pinned_dispatch_fails_closed_without_capacity(self):
        self.current_task = self.assigned("ui_one", "gpt")
        runner = self.runner("ui_one", "gpt", capacity=False)
        with self.assertRaisesRegex(GPTTaskError, "requires shared capacity"):
            runner.execute(self.current_task, "slot-one")
        self.assertIsNone(runner.delivery.snapshot(self.current_task, OWNER))
        self.assertIsNone(self.inbox._store.process_record(self.current_task))

    def test_cannot_run_gpt_adapter_for_claude_pin(self):
        self.current_task = self.assigned("ui_one", "claude")
        runner = self.runner("ui_one", "gpt")
        with self.assertRaisesRegex(GPTTaskError, "provider mismatch"):
            runner.execute(self.current_task, "slot-one")
        self.assertIsNone(runner.delivery.snapshot(self.current_task, OWNER))


if __name__ == "__main__":
    unittest.main()
