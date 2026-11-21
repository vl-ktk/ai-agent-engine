"""WEB-15 Linux SQLite queue and cold-start mock recovery, no live browser."""

from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import unittest

if sys.platform.startswith("linux"):
    import test_chatgpt_web_web12 as web12
    from test_chatgpt_web_web11 import Checkpoints
    from test_main_specialists import OWNER
    from ai_agent_engine import (
        AgentRole, ChatGPTWebConfig, TrustedAgentInstructionPolicy,
        TrustedAgentModelPolicy, TrustedChatGPTProjectBinding,
        TrustedChatGPTProjectRegistry, TrustedProjectModelCatalog,
        WebAgentIdentity, WebAgentModelRegistry, WebAgentSessionRegistry,
        WebInstructionBootstrap,
    )
    from ai_agent_engine.chatgpt_web_core import WebCoreTaskAdapter
    from ai_agent_engine.chatgpt_web_delivery import (
        SQLiteWebDeliveryJournal, WebIdempotentDispatcher,
    )
    from ai_agent_engine.chatgpt_web_failures import (
        GuardedBrowserPort, WebFaultCode, WebFaultGuard, WebFaultHold, WebPortFault,
    )
    from ai_agent_engine.chatgpt_web_handoff import checkpoint_sha256
    from ai_agent_engine.chatgpt_web_recovery import (
        SQLiteWebRecoveryQueue, WebRecoveryCoordinator, WebRecoveryHold,
    )
    from ai_agent_engine.main_shared_capacity import (
        CapacityPolicy, ProviderBudget, SharedCapacity,
    )
    from ai_agent_engine.task_delivery import TaskDeliveryManager
    from ai_agent_engine.task_permissions import (
        ActionGrant, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_state import TaskState
    from ai_agent_engine.task_storage import SCHEMA_VERSION


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux private SQLite and fcntl")
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = web12.CoreHandoffTests(
            "test_approved_task_to_mock_browser_to_strict_v1_response")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.task = self.fx.current
        self.checkpoints = Checkpoints([])
        self.checkpoint_id = "cp_approved"
        self.digest = checkpoint_sha256(self.checkpoints.status,
            self.checkpoints.summary, self.checkpoints.next_action)
        self.db = Path(self.fx.fx.temp.name) / "web15.sqlite"
        self.delivery_db = Path(self.fx.fx.temp.name) / "web13.sqlite"
        self.queue = SQLiteWebRecoveryQueue(self.db)
        self.addCleanup(self.queue.close)
        self.journal = SQLiteWebDeliveryJournal(self.delivery_db)
        self.addCleanup(self.journal.close)
        self._attach_existing_runtime()

    def _attach_existing_runtime(self):
        profile = self.fx.sessions.profile(
            owner_id="vlady", project_id="engine", agent_id="ui_one")
        self.guard = WebFaultGuard(profile)
        underlying = self.fx.ports["ui_one"]
        self.fx.sessions._agents["ui_one"].browser = GuardedBrowserPort(
            profile, underlying, self.guard)
        self.port = underlying
        self.dispatcher = WebIdempotentDispatcher(
            self.fx.adapter, TaskDeliveryManager(self.fx.inbox,
                retry_delay_seconds=0), self.journal, fault_guard=self.guard)
        self.coordinator = WebRecoveryCoordinator(
            self.dispatcher, self.queue, self.checkpoints)

    def enqueue(self):
        self.coordinator.enqueue(task_id=self.task, worker_id="ui_one",
            checkpoint_id=self.checkpoint_id, revision=self.checkpoints.revision,
            checkpoint_sha=self.digest)

    def restart(self):
        """Reopen all three SQLite files and reconstruct trusted registries."""
        self.queue.close()
        self.journal.close()
        self.fx.fx.restart()
        self.fx.inbox = self.fx.fx.inbox
        ports = {}
        guards = {}
        def factory(profile):
            actual = web12.Browser(profile)
            guard = WebFaultGuard(profile)
            ports[profile.agent_id] = actual
            guards[profile.agent_id] = guard
            return GuardedBrowserPort(profile, actual, guard)
        sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant",)),
            browser_factory=factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("vlady", "engine", "gpt-engine"),)),
            project_verifier=web12.Member())
        models = WebAgentModelRegistry(
            sessions,
            project_catalogs=(TrustedProjectModelCatalog("gpt-engine", ("instant",)),),
            selector=web12.Selector(),
            instruction_bootstrap=WebInstructionBootstrap(
                source=web12.Instructions(), grant_verifier=web12.Grants()))
        sessions.register(WebAgentIdentity(
            "vlady", "engine", "ui_one", AgentRole.EXECUTOR,
            declared_actions=("EXTERNAL_REQUEST",)))
        models.register(TrustedAgentModelPolicy(
            "vlady", "engine", "ui_one", "instant", ("instant",)))
        models.register_instructions(TrustedAgentInstructionPolicy(
            "vlady", "engine", "ui_one", "gpt-engine", AgentRole.EXECUTOR,
            1, web12.digest("Approved scoped web instructions"),
            ("EXTERNAL_REQUEST",)))
        gate = TaskPermissionGate(self.fx.inbox, grants=(
            ActionGrant(self.task, "ui_one", ProtectedAction.EXTERNAL_REQUEST),))
        capacity = SharedCapacity(self.fx.inbox,
            CapacityPolicy(1, (ProviderBudget("gpt", 1, 4, 60),)))
        core = WebCoreTaskAdapter(self.fx.inbox, OWNER, sessions, models,
                                  gate, capacity)
        self.fx.sessions, self.fx.models = sessions, models
        self.fx.adapter, self.fx.gate, self.fx.capacity = core, gate, capacity
        self.guard = guards["ui_one"]
        self.port = ports["ui_one"]
        self.queue = SQLiteWebRecoveryQueue(self.db)
        self.addCleanup(self.queue.close)
        self.journal = SQLiteWebDeliveryJournal(self.delivery_db)
        self.addCleanup(self.journal.close)
        self.dispatcher = WebIdempotentDispatcher(
            core, TaskDeliveryManager(self.fx.inbox, retry_delay_seconds=0),
            self.journal, fault_guard=self.guard)
        self.coordinator = WebRecoveryCoordinator(
            self.dispatcher, self.queue, self.checkpoints)

    def test_unsent_task_survives_cold_restart_and_runs_once(self):
        self.enqueue()
        self.assertEqual(self.coordinator.inventory()[0].state, "READY")
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "READY")
        done = self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        self.assertTrue(done.executed)
        self.assertEqual(done.receipt.state, "ACKED")
        self.assertEqual(len(self.port.turns), 1)
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "COMPLETED")
        with self.assertRaises(WebRecoveryHold):
            self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        self.assertEqual(len(self.port.turns), 0)

    def test_inflight_unknown_outcome_persists_hold_across_restarts(self):
        self.enqueue()
        self.queue.transition(self.task, "PENDING", "IN_FLIGHT")
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "HOLD")
        self.assertEqual(self.guard.snapshot().code, WebFaultCode.UNKNOWN_SEND)
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "HOLD")
        with self.assertRaises(WebRecoveryHold):
            self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        with self.assertRaises(WebRecoveryHold):
            self.queue.transition(self.task, "HOLD", "PENDING")
        self.assertEqual(len(self.port.turns), 0)

    def test_browser_fault_preserves_task_capacity_and_durable_agent_hold(self):
        self.enqueue()
        def fail(*, model_id, messages):
            self.port.turns.append(messages)
            raise WebPortFault(WebFaultCode.CAPTCHA)
        self.port.complete = fail
        with self.assertRaises(WebRecoveryHold):
            self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.RUNNING)
        self.assertIsNotNone(self.fx.inbox._store.capacity_reservation(self.task))
        self.assertEqual(len(self.port.turns), 1)
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "HOLD")
        self.assertEqual(self.guard.snapshot().code, WebFaultCode.CAPTCHA)
        self.assertEqual(len(self.port.turns), 0)

    def test_checkpoint_change_blocks_previously_queued_task(self):
        self.enqueue()
        self.checkpoints.revision += 1
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "HOLD")
        self.assertEqual(len(self.port.turns), 0)

    def test_paused_checkpoint_cannot_authorize_new_queue_send(self):
        self.checkpoints.status = "paused"
        self.digest = checkpoint_sha256(self.checkpoints.status,
            self.checkpoints.summary, self.checkpoints.next_action)
        with self.assertRaises(WebRecoveryHold):
            self.enqueue()
        self.assertEqual(self.queue.entries("vlady"), ())
        self.assertIsNone(self.dispatcher.delivery.snapshot(self.task, OWNER))

    def test_cross_scope_and_rewritten_queue_entry_fail_closed(self):
        self.enqueue()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE queue SET checkpoint_sha=? WHERE task=?",
                       ("0" * 64, self.task))
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "HOLD")

    def test_queue_is_metadata_only_and_original_task_db_unchanged(self):
        self.enqueue()
        with sqlite3.connect(self.db) as db:
            text = " ".join(str(x) for row in db.execute(
                "SELECT * FROM queue") for x in row)
            self.assertNotIn("Private", text)
            self.assertNotIn(self.checkpoints.summary, text)
            self.assertNotIn("Approved scoped web instructions", text)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (1,))
        self.assertEqual(self.fx.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (SCHEMA_VERSION,))

    def test_duplicate_enqueue_same_pin_and_no_double_delivery(self):
        self.enqueue()
        self.enqueue()
        self.assertEqual(len(self.queue.entries("vlady")), 1)
        self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        with self.assertRaises(WebRecoveryHold):
            self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        self.assertEqual(len(self.port.turns), 1)

    def test_concurrent_resume_uses_one_agent_fifo_and_one_browser_call(self):
        self.enqueue()
        self.port.entered = Event()
        self.port.release = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.coordinator.dispatch_next,
                                task_id=self.task, worker_id="ui_one")
            self.assertTrue(self.port.entered.wait(5))
            duplicate = pool.submit(self.coordinator.dispatch_next,
                                    task_id=self.task, worker_id="ui_one")
            self.port.release.set()
            self.assertTrue(first.result(timeout=5).executed)
            with self.assertRaises(WebRecoveryHold):
                duplicate.result(timeout=5)
        self.assertEqual(len(self.port.turns), 1)
        self.assertEqual(self.coordinator.inventory()[0].state, "COMPLETED")

    def test_schema_permissions_and_duplicate_writer_fail_closed(self):
        with self.assertRaises(WebRecoveryHold):
            SQLiteWebRecoveryQueue(self.db)
        self.queue.close()
        os.chmod(self.db, 0o644)
        with self.assertRaises(WebRecoveryHold):
            SQLiteWebRecoveryQueue(self.db)
        os.chmod(self.db, 0o600)
        foreign = Path(self.fx.fx.temp.name) / "foreign-recovery.sqlite"
        with sqlite3.connect(foreign) as db:
            db.execute("CREATE TABLE other (value TEXT)")
        os.chmod(foreign, 0o600)
        with self.assertRaises(WebRecoveryHold):
            SQLiteWebRecoveryQueue(foreign)

    def test_direct_fault_observation_persists_agent_hold_before_any_delivery(self):
        self.enqueue()
        def fail(*, model_id, messages):
            raise WebPortFault(WebFaultCode.SESSION_EXPIRED)
        self.port.complete = fail
        with self.assertRaises(WebFaultHold):
            self.fx.sessions._agents["ui_one"].browser.complete(
                model_id="instant", messages=())
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT fault FROM agents").fetchone(),
                             ("session_expired",))
        self.assertIsNone(self.dispatcher.delivery.snapshot(self.task, OWNER))
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "HOLD")
        self.assertEqual(self.guard.snapshot().code, WebFaultCode.SESSION_EXPIRED)
        self.assertEqual(len(self.port.turns), 0)

    def test_proven_pre_send_capacity_deferral_can_resume_once_after_restart(self):
        self.enqueue()
        other = self.fx.assign("ui_two")
        reservation = self.fx.capacity.acquire(other, OWNER, worker_id="ui_two")
        self.assertTrue(reservation.admitted)
        delayed = self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        self.assertFalse(delayed.executed)
        self.assertEqual(delayed.receipt.state, "DEFERRED")
        self.assertEqual(self.queue.entries("vlady")[0][-1], "PENDING")
        self.assertEqual(len(self.port.turns), 0)
        self.fx.inbox.transition_state(other, TaskState.FAILED, OWNER,
                                       actor_id="ui_two")
        self.fx.capacity.release(other, OWNER, worker_id="ui_two",
                                 reservation_id=reservation.reservation_id)
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "READY")
        done = self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        self.assertTrue(done.executed)
        self.assertEqual(done.receipt.attempt, 2)
        self.assertEqual(len(self.port.turns), 1)

    def test_queue_capacity_is_bounded_and_does_not_mutate_existing_task(self):
        path = Path(self.fx.fx.temp.name) / "bounded-queue.sqlite"
        bounded = SQLiteWebRecoveryQueue(path, max_pending=1)
        self.addCleanup(bounded.close)
        meta = dict(owner="vlady", project="engine", agent="ui_one",
            chat_project="gpt-engine", profile="profile-abc",
            mailbox="mailbox-abc", checkpoint="cp-one", revision=1,
            checkpoint_sha="a" * 64, request_sha="b" * 64)
        bounded.enqueue(task="task_" + "1" * 32, **meta)
        with self.assertRaises(WebRecoveryHold):
            bounded.enqueue(task="task_" + "2" * 32, **meta)
        self.assertEqual(len(bounded.entries("vlady")), 1)

    def test_foreign_key_corruption_blocks_recovery_database_reopen(self):
        self.enqueue()
        self.queue.close()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE queue SET project='foreign' WHERE task=?",
                       (self.task,))
        with self.assertRaises(WebRecoveryHold):
            SQLiteWebRecoveryQueue(self.db)

    def test_bound_size_rejects_unlimited_queue(self):
        with self.assertRaises(ValueError):
            SQLiteWebRecoveryQueue(self.db, max_pending=10_000)

    def test_real_process_exit_during_browser_keeps_queue_and_no_second_send(self):
        self.enqueue()
        marker = Path(self.fx.fx.temp.name) / "web15-crash-marker"
        task_db, workspaces = self.fx.fx.db, self.fx.fx.workspaces
        self.queue.close()
        self.journal.close()
        self.fx.inbox.close()
        script = r'''
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd() / "tests"))
import test_chatgpt_web_web12 as mock
from test_main_specialists import OWNER
from ai_agent_engine import (AgentRole, ChatGPTWebConfig,
    TrustedAgentInstructionPolicy, TrustedAgentModelPolicy,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
    TrustedProjectModelCatalog, WebAgentIdentity, WebAgentModelRegistry,
    WebAgentSessionRegistry, WebInstructionBootstrap)
from ai_agent_engine.chatgpt_web_core import WebCoreTaskAdapter
from ai_agent_engine.chatgpt_web_delivery import SQLiteWebDeliveryJournal, WebIdempotentDispatcher
from ai_agent_engine.chatgpt_web_failures import GuardedBrowserPort, WebFaultGuard
from ai_agent_engine.chatgpt_web_handoff import ScopedCheckpointSnapshot
from ai_agent_engine.chatgpt_web_recovery import SQLiteWebRecoveryQueue, WebRecoveryCoordinator
from ai_agent_engine.main_shared_capacity import CapacityPolicy, ProviderBudget, SharedCapacity
from ai_agent_engine.task_api import TaskInbox
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_delivery import TaskDeliveryManager
from ai_agent_engine.task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from ai_agent_engine.task_storage import SQLiteTaskStore

task_db, workspaces, delivery_db, recovery_db, task, marker = sys.argv[1:]
class CrashPort:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
    def complete(self, *, model_id, messages):
        with open(marker, "ab") as output:
            output.write(b"browser-entered\n")
            output.flush()
            os.fsync(output.fileno())
        os._exit(37)

class CP:
    def read_checkpoint(self, *, binding, profile, task_id, checkpoint_id):
        return ScopedCheckpointSnapshot(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key, profile.mailbox_key,
            task_id, checkpoint_id, 7, "in_progress",
            "Validated step three is finished", "Run remaining isolated checks",
            True, True)

inbox = TaskInbox(store=SQLiteTaskStore(task_db),
                  workspace_root=WorkspaceRoot(Path(workspaces)))
guards = {}
def factory(profile):
    guard = WebFaultGuard(profile)
    guards[profile.agent_id] = guard
    return GuardedBrowserPort(profile, CrashPort(profile), guard)
sessions = WebAgentSessionRegistry(
    ChatGPTWebConfig(enabled=True, models=("instant",)),
    browser_factory=factory,
    project_registry=TrustedChatGPTProjectRegistry((
        TrustedChatGPTProjectBinding("vlady", "engine", "gpt-engine"),)),
    project_verifier=mock.Member())
models = WebAgentModelRegistry(
    sessions, project_catalogs=(TrustedProjectModelCatalog("gpt-engine", ("instant",)),),
    selector=mock.Selector(), instruction_bootstrap=WebInstructionBootstrap(
        source=mock.Instructions(), grant_verifier=mock.Grants()))
sessions.register(WebAgentIdentity("vlady", "engine", "ui_one", AgentRole.EXECUTOR,
    declared_actions=("EXTERNAL_REQUEST",)))
models.register(TrustedAgentModelPolicy("vlady", "engine", "ui_one", "instant", ("instant",)))
models.register_instructions(TrustedAgentInstructionPolicy(
    "vlady", "engine", "ui_one", "gpt-engine", AgentRole.EXECUTOR,
    1, mock.digest("Approved scoped web instructions"), ("EXTERNAL_REQUEST",)))
gate = TaskPermissionGate(inbox, grants=(
    ActionGrant(task, "ui_one", ProtectedAction.EXTERNAL_REQUEST),))
capacity = SharedCapacity(inbox, CapacityPolicy(1, (ProviderBudget("gpt",1,4,60),)))
core = WebCoreTaskAdapter(inbox, OWNER, sessions, models, gate, capacity)
delivery = TaskDeliveryManager(inbox, retry_delay_seconds=0)
journal = SQLiteWebDeliveryJournal(delivery_db)
queue = SQLiteWebRecoveryQueue(recovery_db)
dispatcher = WebIdempotentDispatcher(core, delivery, journal,
                                     fault_guard=guards["ui_one"])
WebRecoveryCoordinator(dispatcher, queue, CP()).dispatch_next(
    task_id=task, worker_id="ui_one")
sys.exit(91)
'''
        proc = subprocess.run(
            [sys.executable, "-c", script, str(task_db), str(workspaces),
             str(self.delivery_db), str(self.db), self.task, str(marker)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(proc.returncode, 37, proc.stderr)
        self.assertEqual(marker.read_bytes(), b"browser-entered\n")
        self.restart()
        self.assertEqual(self.coordinator.inventory()[0].state, "HOLD")
        self.assertEqual(self.guard.snapshot().code, WebFaultCode.UNKNOWN_SEND)
        self.assertEqual(self.dispatcher.delivery.snapshot(
            self.task, OWNER).status, "active")
        self.assertEqual(self.journal.snapshot(owner_id="vlady",
            project_id="engine", agent_id="ui_one", task_id=self.task).state,
            "ACTIVE")
        with self.assertRaises(WebRecoveryHold):
            self.coordinator.dispatch_next(task_id=self.task, worker_id="ui_one")
        self.assertEqual(marker.read_bytes(), b"browser-entered\n")
        self.assertEqual(len(self.port.turns), 0)


if __name__ == "__main__":
    unittest.main()
