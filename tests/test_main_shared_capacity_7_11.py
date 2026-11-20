"""7.11: shared controller slots, provider budgets and safe waiting."""

from __future__ import annotations

import sqlite3
import sys
from threading import Barrier, Thread
import unittest

if sys.platform.startswith("linux"):
    from ai_agent_engine.main_shared_capacity import (
        CapacityDenied, CapacityPolicy, ProviderBudget, SharedCapacity,
    )
    from ai_agent_engine.task_api import OwnerScope, TaskInbox
    from ai_agent_engine.task_assignment import WorkspaceRoot
    from ai_agent_engine.task_permissions import ProtectedAction
    from ai_agent_engine.task_state import TaskState
    from ai_agent_engine.task_storage import SQLiteTaskStore, SCHEMA_VERSION
    from test_main_specialists import OWNER, FOREIGN, SpecialistSQLiteTests


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private SQLite only")
class SharedCapacityTests(unittest.TestCase):
    def setUp(self):
        self.fx = SpecialistSQLiteTests("test_catalog_team_approval_exact_replay_no_activation")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.inbox = self.fx.inbox
        self.now = [1000.0]

    def scheduler(self, cpu=1, gpt_parallel=2, gpt_calls=3,
                  claude_parallel=2, claude_calls=3, *, with_claude=True):
        budgets = [ProviderBudget("gpt", gpt_parallel, gpt_calls, 60)]
        if with_claude:
            budgets.append(ProviderBudget("claude", claude_parallel, claude_calls, 60))
        return SharedCapacity(self.inbox, CapacityPolicy(cpu, tuple(budgets)),
                              clock=lambda: self.now[0])

    def assigned(self, agent="ui_one", provider="gpt"):
        task_id = self.fx.task()
        action = (ProtectedAction.WRITE_WORKSPACE if agent == "ui_one"
                  else ProtectedAction.EXECUTE_TOOL)
        reason = "approved_fallback" if provider == "claude" else None
        self.fx.assign(task_id, candidate=self.fx.candidate(agent, provider, reason),
                       req=self.fx.requirements(task_id, actions=(action,)),
                       grants=(self.fx.grant(task_id, agent, action),))
        return task_id

    def fail(self, task_id, worker):
        self.inbox.transition_state(task_id, TaskState.FAILED, OWNER,
                                    actor_id=worker)

    def test_policy_rejects_missing_unbounded_or_duplicate_provider(self):
        with self.assertRaises(ValueError):
            CapacityPolicy(0, (ProviderBudget("gpt", 1, 1, 60),))
        with self.assertRaises(ValueError):
            ProviderBudget("unknown", 1, 1, 60)
        with self.assertRaises(ValueError):
            CapacityPolicy(1, (ProviderBudget("gpt", 1, 1, 60),
                               ProviderBudget("gpt", 1, 1, 60)))
        with self.assertRaises(ValueError):
            ProviderBudget("gpt", True, 1, 60)
        with self.assertRaises(ValueError):
            ProviderBudget("gpt", 1, 0, 60)

    def test_cpu_pool_shared_by_gpt_and_claude_without_avatar_processes(self):
        gpt = self.assigned("ui_one", "gpt")
        claude = self.assigned("ui_two", "claude")
        manager = self.scheduler(cpu=1)
        ticket = manager.acquire(gpt, OWNER, worker_id="ui_one")
        self.assertTrue(ticket.admitted)
        self.assertEqual(manager.acquire(gpt, OWNER, worker_id="ui_one").reason,
                         "task_active")
        waiting = manager.acquire(claude, OWNER, worker_id="ui_two")
        self.assertEqual(waiting.reason, "cpu_busy")
        self.assertIsNone(waiting.reservation_id)
        self.assertEqual(self.inbox.state(claude, OWNER), TaskState.ASSIGNED)
        self.assertEqual([c.task_id for c in manager.waiting(OWNER)], [claude])
        with self.assertRaises(CapacityDenied):
            manager.release(gpt, OWNER, worker_id="ui_one",
                            reservation_id=ticket.reservation_id)
        self.fail(gpt, "ui_one")
        self.assertTrue(manager.release(gpt, OWNER, worker_id="ui_one",
                                        reservation_id=ticket.reservation_id))
        self.assertFalse(manager.release(gpt, OWNER, worker_id="ui_one",
                                         reservation_id=ticket.reservation_id))
        self.assertTrue(manager.acquire(claude, OWNER, worker_id="ui_two").admitted)

    def test_provider_parallel_slots_and_quota_persist_after_restart(self):
        first = self.assigned("ui_one")
        second = self.assigned("ui_two")
        manager = self.scheduler(cpu=2, gpt_parallel=1, gpt_calls=1)
        admitted = manager.acquire(first, OWNER, worker_id="ui_one")
        self.assertEqual(manager.acquire(second, OWNER, worker_id="ui_two").reason,
                         "provider_busy")
        self.fail(first, "ui_one")
        manager.release(first, OWNER, worker_id="ui_one",
                        reservation_id=admitted.reservation_id)
        result = manager.acquire(second, OWNER, worker_id="ui_two")
        self.assertEqual(result.reason, "provider_quota")
        self.assertAlmostEqual(result.retry_after_seconds, 60.0)
        self.fx.restart()
        self.inbox = self.fx.inbox
        manager = self.scheduler(cpu=2, gpt_parallel=1, gpt_calls=1)
        self.assertEqual(manager.acquire(second, OWNER, worker_id="ui_two").reason,
                         "provider_quota")
        self.now[0] += 60
        self.assertTrue(manager.acquire(second, OWNER, worker_id="ui_two").admitted)
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT count(*) FROM task_capacity_reservations").fetchone(), (2,))

    def test_unknown_crash_keeps_capacity_reserved_until_verified_stop(self):
        first = self.assigned("ui_one")
        second = self.assigned("ui_two")
        manager = self.scheduler(cpu=1)
        ticket = manager.acquire(first, OWNER, worker_id="ui_one")
        self.fx.restart()
        self.inbox = self.fx.inbox
        manager = self.scheduler(cpu=1)
        self.now[0] += 500
        self.assertEqual(manager.acquire(second, OWNER, worker_id="ui_two").reason,
                         "cpu_busy")
        with self.assertRaises(CapacityDenied):
            manager.release(first, OWNER, worker_id="ui_one",
                            reservation_id=ticket.reservation_id)
        self.fail(first, "ui_one")
        self.assertTrue(manager.release(first, OWNER, worker_id="ui_one",
                                        reservation_id=ticket.reservation_id))
        self.assertTrue(manager.acquire(second, OWNER, worker_id="ui_two").admitted)

    def test_running_or_unverified_process_never_releases_cpu(self):
        task_id = self.assigned()
        manager = self.scheduler()
        ticket = manager.acquire(task_id, OWNER, worker_id="ui_one")
        self.inbox.transition_state(task_id, TaskState.RUNNING, OWNER,
                                    actor_id="ui_one")
        with self.assertRaises(CapacityDenied):
            manager.release(task_id, OWNER, worker_id="ui_one",
                            reservation_id=ticket.reservation_id)
        self.inbox.transition_state(task_id, TaskState.FAILED, OWNER,
                                    actor_id="ui_one")
        with self.inbox._store._db() as db:
            db.execute("""INSERT INTO task_process_runs
                (task_id,worker_id,pid,pgid,sid,start_ticks,boot_id,launch_kind,status)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (task_id, "ui_one", 1234, 1234, 1234, 1, "boot", "gated", "active"))
        with self.assertRaises(CapacityDenied):
            manager.release(task_id, OWNER, worker_id="ui_one",
                            reservation_id=ticket.reservation_id)
        self.inbox._store.mark_process_stopped(task_id, 1234, 1, "boot")
        self.assertTrue(manager.release(task_id, OWNER, worker_id="ui_one",
                                        reservation_id=ticket.reservation_id))

    def test_scope_assignment_and_disabled_provider_fail_closed(self):
        task_id = self.assigned("ui_one", "claude")
        manager = self.scheduler(with_claude=False)
        self.assertEqual(manager.acquire(task_id, OWNER, worker_id="ui_one").reason,
                         "provider_not_enabled")
        with self.assertRaises(CapacityDenied):
            manager.acquire(task_id, FOREIGN, worker_id="ui_one")
        with self.assertRaises(CapacityDenied):
            manager.acquire(task_id, OWNER, worker_id="ui_two")
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT count(*) FROM task_capacity_reservations").fetchone(), (0,))

    def test_two_contenders_cannot_overbook_shared_cpu_slot(self):
        tasks = (self.assigned("ui_one"), self.assigned("ui_two"))
        manager = self.scheduler(cpu=1)
        barrier = Barrier(2)
        decisions = []
        def worker(task, agent):
            barrier.wait(timeout=5)
            decisions.append(manager.acquire(task, OWNER, worker_id=agent))
        t1 = Thread(target=worker, args=(tasks[0], "ui_one"))
        t2 = Thread(target=worker, args=(tasks[1], "ui_two"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(sorted(x.reason for x in decisions), ["admitted", "cpu_busy"])

    def test_backwards_clock_cannot_reset_shared_provider_budget(self):
        first = self.assigned("ui_one")
        second = self.assigned("ui_two")
        manager = self.scheduler(cpu=2)
        self.assertTrue(manager.acquire(first, OWNER, worker_id="ui_one").admitted)
        self.now[0] -= 1
        self.assertEqual(manager.acquire(second, OWNER, worker_id="ui_two").reason,
                         "clock_rollback")

    def test_waiting_queue_preserves_priority_and_receipt_order(self):
        earlier = self.fx.task()
        later = self.fx.task()
        self.inbox.set_priority(later, 50, OWNER)
        self.fx.assign(earlier)
        self.fx.assign(later, candidate=self.fx.candidate("ui_two"),
                       req=self.fx.requirements(later,
                           actions=(ProtectedAction.EXECUTE_TOOL,)),
                       grants=(self.fx.grant(later, "ui_two",
                           ProtectedAction.EXECUTE_TOOL),))
        self.assertEqual([x.task_id for x in self.scheduler().waiting(OWNER)],
                         [later, earlier])

    def test_wrapper_runs_once_and_returns_wait_without_second_callback(self):
        tasks = (self.assigned("ui_one"), self.assigned("ui_two"))
        manager = self.scheduler(cpu=1)
        first = manager.acquire(tasks[0], OWNER, worker_id="ui_one")
        called = []
        result, payload = manager.run(tasks[1], OWNER, worker_id="ui_two",
                                      operation=lambda: called.append("never"))
        self.assertEqual((result.reason, payload, called), ("cpu_busy", None, []))
        self.fail(tasks[0], "ui_one")
        manager.release(tasks[0], OWNER, worker_id="ui_one",
                        reservation_id=first.reservation_id)
        def done():
            called.append("once")
            self.fail(tasks[1], "ui_two")
            return "result"
        result, payload = manager.run(tasks[1], OWNER, worker_id="ui_two",
                                      operation=done)
        self.assertEqual((result.admitted, payload, called),
                         (True, "result", ["once"]))
        self.assertIsNone(self.inbox._store.capacity_reservation(tasks[1]))

    def test_v16_migration_preserves_tasks_and_creates_empty_capacity_ledger(self):
        task_id = self.assigned()
        self.fx.inbox.close()
        with sqlite3.connect(self.fx.db) as db:
            db.execute("DROP TABLE task_capacity_reservations")
            db.execute("PRAGMA user_version=16")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.fx.db),
                               workspace_root=WorkspaceRoot(self.fx.workspaces))
        self.fx.inbox = self.inbox
        self.assertEqual(self.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (SCHEMA_VERSION,))
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)
        self.assertEqual(self.inbox._store._db().execute(
            "SELECT count(*) FROM task_capacity_reservations").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
