"""WEB-13: durable at-most-once mock Web delivery on the existing task core."""

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import unittest

if sys.platform.startswith("linux"):
    import test_chatgpt_web_web12 as web12_fixtures
    from ai_agent_engine.chatgpt_web_core import WebCoreTaskAdapter
    from ai_agent_engine.chatgpt_web_delivery import (
        SQLiteWebDeliveryJournal, WebDeliveryHold, WebIdempotentDispatcher,
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
    from test_main_specialists import FOREIGN, OWNER


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux durable task core only")
class WebDeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = web12_fixtures.CoreHandoffTests(
            "test_approved_task_to_mock_browser_to_strict_v1_response")
        fixture.setUp()
        self.fx = fixture
        self.addCleanup(fixture.doCleanups)
        self.path = Path(fixture.fx.temp.name) / "web13.sqlite"
        self.journal = SQLiteWebDeliveryJournal(self.path)
        self.addCleanup(self.journal.close)
        self.manager = TaskDeliveryManager(fixture.inbox, retry_delay_seconds=0)
        self.dispatcher = WebIdempotentDispatcher(
            fixture.adapter, self.manager, self.journal, max_attempts=3)
        self.task = fixture.current
        self.raw = fixture.request()

    def dispatch(self, **kwargs):
        args = dict(raw_request=self.raw, task_id=self.task,
                    worker_id="ui_one")
        args.update(kwargs)
        return self.dispatcher.dispatch(**args)

    def snapshot(self):
        return self.journal.snapshot(owner_id="vlady", project_id="engine",
                                     agent_id="ui_one", task_id=self.task)

    def test_task_to_browser_to_durable_ack_without_raw_secrets(self):
        done = self.dispatch()
        self.assertTrue(done.executed)
        self.assertEqual(done.reason, "completed")
        self.assertEqual(done.receipt.state, "ACKED")
        self.assertEqual(done.receipt.attempt, 1)
        self.assertEqual(done.response.text, "Approved mock answer")
        self.assertEqual(done.receipt.response_sha256,
            sha256(done.response.to_bytes()).hexdigest())
        self.assertEqual(self.manager.snapshot(self.task, OWNER).status, "completed")
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.SUCCEEDED)
        self.assertIsNone(self.fx.inbox._store.capacity_reservation(self.task))
        self.assertTrue(done.receipt.correlation_id.startswith("web13_"))
        with sqlite3.connect(self.path) as db:
            raw = " ".join(str(x) for row in db.execute(
                "SELECT * FROM web13_delivery") for x in row)
            self.assertNotIn("Approved mock answer", raw)
            self.assertNotIn("Private", raw)
            self.assertNotIn("Approved scoped web instructions", raw)
            self.assertNotIn(self.fx.inbox._store.delivery(self.task)[2], raw)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (1,))
        self.assertEqual(self.fx.inbox._store._db().execute(
            "PRAGMA user_version").fetchone(), (SCHEMA_VERSION,))

    def test_duplicate_after_completed_ack_is_inert(self):
        first = self.dispatch()
        second = self.dispatch()
        self.assertFalse(second.executed)
        self.assertEqual(second.reason, "already_reserved")
        self.assertEqual(second.receipt, first.receipt)
        self.assertEqual(len(self.fx.ports["ui_one"].turns), 1)

    def test_capacity_denials_use_bounded_pre_send_retries(self):
        other = self.fx.assign("ui_two")
        ticket = self.fx.capacity.acquire(other, OWNER, worker_id="ui_two")
        self.assertTrue(ticket.admitted)
        for n, expected in enumerate(("DEFERRED", "DEFERRED", "EXHAUSTED"), 1):
            outcome = self.dispatch()
            self.assertFalse(outcome.executed)
            self.assertEqual(outcome.reason, "capacity_cpu_busy")
            self.assertEqual(outcome.receipt.state, expected)
            self.assertEqual(outcome.receipt.attempt, n)
        self.assertFalse(self.fx.ports["ui_one"].turns)
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.ASSIGNED)
        self.assertEqual(self.manager.snapshot(self.task, OWNER).status, "exhausted")
        repeated = self.dispatch()
        self.assertEqual((repeated.executed, repeated.receipt.attempt), (False, 3))

    def test_retry_after_temporary_cpu_busy_can_succeed_once(self):
        other = self.fx.assign("ui_two")
        ticket = self.fx.capacity.acquire(other, OWNER, worker_id="ui_two")
        self.assertFalse(self.dispatch().executed)
        self.fx.inbox.transition_state(other, TaskState.FAILED, OWNER,
                                       actor_id="ui_two")
        self.fx.capacity.release(other, OWNER, worker_id="ui_two",
                                  reservation_id=ticket.reservation_id)
        success = self.dispatch()
        self.assertTrue(success.executed)
        self.assertEqual((success.receipt.attempt, success.receipt.state),
                         (2, "ACKED"))
        self.assertEqual(len(self.fx.ports["ui_one"].turns), 1)

    def test_wrong_owner_and_tampered_request_do_not_reserve(self):
        wrong = WebCoreTaskAdapter(self.fx.inbox, FOREIGN, self.fx.sessions,
                                   self.fx.models, self.fx.gate,
                                   self.fx.capacity)
        attacker = WebIdempotentDispatcher(wrong, self.manager, self.journal)
        with self.assertRaises(WebDeliveryHold):
            attacker.dispatch(self.raw, task_id=self.task, worker_id="ui_one")
        forged = json.loads(self.raw)
        forged["permissions"]["required_actions"] = ["read_secret"]
        with self.assertRaises(WebDeliveryHold):
            self.dispatch(raw_request=json.dumps(forged).encode())
        self.assertIsNone(self.manager.snapshot(self.task, OWNER))
        self.assertIsNone(self.snapshot())

    def test_unknown_model_refused_before_delivery_token(self):
        with self.assertRaises(WebDeliveryHold):
            self.dispatch(requested_model_id="unsupported")
        self.assertIsNone(self.manager.snapshot(self.task, OWNER))

    def test_browser_failure_keeps_active_lease_and_slot(self):
        self.fx.ports["ui_one"].throw = True
        with self.assertRaises(WebDeliveryHold):
            self.dispatch()
        self.assertEqual(self.snapshot().state, "ACTIVE")
        self.assertEqual(self.manager.snapshot(self.task, OWNER).status, "active")
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.RUNNING)
        self.assertIsNotNone(self.fx.inbox._store.capacity_reservation(self.task))
        self.assertEqual(len(self.fx.ports["ui_one"].turns), 1)
        refused = self.dispatch()
        self.assertFalse(refused.executed)
        self.assertEqual(refused.receipt.state, "ACTIVE")
        self.assertEqual(len(self.fx.ports["ui_one"].turns), 1)

    def test_concurrent_double_delivery_causes_one_browser_turn(self):
        entered, release = Event(), Event()
        self.fx.ports["ui_one"].entered = entered
        self.fx.ports["ui_one"].release = release
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.dispatch)
            self.assertTrue(entered.wait(5))
            duplicate = self.dispatch()
            self.assertFalse(duplicate.executed)
            self.assertEqual(duplicate.receipt.state, "ACTIVE")
            release.set()
            self.assertTrue(first.result(timeout=5).executed)
        self.assertEqual(len(self.fx.ports["ui_one"].turns), 1)

    def test_lease_expiry_is_hold_not_permission_to_resend(self):
        ticks = iter((1000, 1002))
        dispatcher = WebIdempotentDispatcher(
            self.fx.adapter, self.manager, self.journal,
            lease_seconds=1, clock=lambda: next(ticks))
        with self.assertRaises(WebDeliveryHold):
            dispatcher.dispatch(self.raw, task_id=self.task, worker_id="ui_one")
        self.assertEqual(self.snapshot().state, "ACTIVE")
        self.assertFalse(self.fx.ports["ui_one"].turns)
        self.assertFalse(self.dispatch().executed)

    def test_ack_failure_after_verified_response_never_resends(self):
        old_report = self.manager.report
        self.manager.report = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("test crash after state SUCCEEDED"))
        with self.assertRaises(WebDeliveryHold):
            self.dispatch()
        self.manager.report = old_report
        self.assertEqual(self.snapshot().state, "VERIFIED")
        self.assertIsNotNone(self.snapshot().response_sha256)
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.SUCCEEDED)
        self.assertEqual(self.manager.snapshot(self.task, OWNER).status, "active")
        # The task has reached SUCCEEDED, but its delivery ACK is missing:
        # the existing durable ledger rejects any further reservation.
        with self.assertRaises(WebDeliveryHold):
            self.dispatch()
        self.assertEqual(len(self.fx.ports["ui_one"].turns), 1)

    def test_mutated_payload_after_safe_deferral_is_fenced(self):
        other = self.fx.assign("ui_two")
        self.fx.capacity.acquire(other, OWNER, worker_id="ui_two")
        self.assertEqual(self.dispatch().receipt.state, "DEFERRED")
        forged = json.loads(self.raw)
        forged["task"]["prompt"] = "unreviewed"
        with self.assertRaises(WebDeliveryHold):
            self.dispatch(raw_request=json.dumps(forged).encode())
        self.assertEqual(self.snapshot().state, "DEFERRED")

    def test_journal_restart_keeps_completed_receipt_without_text(self):
        previous = self.dispatch()
        self.journal.close()
        self.journal = SQLiteWebDeliveryJournal(self.path)
        self.addCleanup(self.journal.close)
        self.dispatcher = WebIdempotentDispatcher(
            self.fx.adapter, self.manager, self.journal)
        again = self.dispatch()
        self.assertEqual(again.receipt, previous.receipt)
        self.assertFalse(again.executed)

    def test_real_process_crash_inside_browser_does_not_resend_after_restart(self):
        """A child os._exit is not caught by Python finalizers or context managers."""
        marker = Path(self.fx.fx.temp.name) / "browser-started.marker"
        db_path = self.fx.fx.db
        workspace_path = self.fx.fx.workspaces
        # Release the exclusive locks, so the child really reopens SQLite.
        self.journal.close()
        self.fx.inbox.close()
        script = r'''
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_chatgpt_web_web12 import Member, Selector, Instructions, Grants, digest
from test_main_specialists import OWNER
from ai_agent_engine import (AgentRole, ChatGPTWebConfig,
    TrustedAgentInstructionPolicy, TrustedAgentModelPolicy,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
    TrustedProjectModelCatalog, WebAgentIdentity, WebAgentModelRegistry,
    WebAgentSessionRegistry, WebInstructionBootstrap)
from ai_agent_engine.chatgpt_web_core import WebCoreTaskAdapter
from ai_agent_engine.chatgpt_web_delivery import SQLiteWebDeliveryJournal, WebIdempotentDispatcher
from ai_agent_engine.main_shared_capacity import CapacityPolicy, ProviderBudget, SharedCapacity
from ai_agent_engine.task_api import TaskInbox
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_delivery import TaskDeliveryManager
from ai_agent_engine.task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from ai_agent_engine.task_request import build_task_request
from ai_agent_engine.task_storage import SQLiteTaskStore

db_path, workspace_path, journal_path, task_id, marker_path = sys.argv[1:]
class CrashPort:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
    def complete(self, *, model_id, messages):
        with open(marker_path, "ab") as output:
            output.write(b"browser-entered\n")
            output.flush()
            os.fsync(output.fileno())
        os._exit(37)

inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                  workspace_root=WorkspaceRoot(Path(workspace_path)))
sessions = WebAgentSessionRegistry(
    ChatGPTWebConfig(enabled=True, models=("instant",)),
    browser_factory=CrashPort,
    project_registry=TrustedChatGPTProjectRegistry((
        TrustedChatGPTProjectBinding("vlady", "engine", "gpt-engine"),)),
    project_verifier=Member())
models = WebAgentModelRegistry(
    sessions, project_catalogs=(TrustedProjectModelCatalog("gpt-engine", ("instant",)),),
    selector=Selector(), instruction_bootstrap=WebInstructionBootstrap(
        source=Instructions(), grant_verifier=Grants()))
sessions.register(WebAgentIdentity("vlady", "engine", "ui_one", AgentRole.EXECUTOR,
                                  declared_actions=("EXTERNAL_REQUEST",)))
models.register(TrustedAgentModelPolicy("vlady", "engine", "ui_one", "instant", ("instant",)))
models.register_instructions(TrustedAgentInstructionPolicy(
    "vlady", "engine", "ui_one", "gpt-engine", AgentRole.EXECUTOR, 1,
    digest("Approved scoped web instructions"), ("EXTERNAL_REQUEST",)))
gate = TaskPermissionGate(inbox, grants=(
    ActionGrant(task_id, "ui_one", ProtectedAction.EXTERNAL_REQUEST),))
capacity = SharedCapacity(inbox, CapacityPolicy(1, (ProviderBudget("gpt", 1, 4, 60),)))
core = WebCoreTaskAdapter(inbox, OWNER, sessions, models, gate, capacity)
delivery = TaskDeliveryManager(inbox, retry_delay_seconds=0)
journal = SQLiteWebDeliveryJournal(journal_path)
raw = build_task_request(inbox, OWNER, task_id, role=AgentRole.EXECUTOR,
    role_instructions="", required_actions=(ProtectedAction.EXTERNAL_REQUEST,)).to_bytes()
WebIdempotentDispatcher(core, delivery, journal).dispatch(
    raw, task_id=task_id, worker_id="ui_one")
sys.exit(91)
'''
        proc = subprocess.run(
            [sys.executable, "-c", script, str(db_path), str(workspace_path),
             str(self.path), self.task, str(marker)],
            cwd=Path(__file__).resolve().parents[1],
            check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 37, proc.stderr)
        self.assertEqual(marker.read_bytes(), b"browser-entered\n")
        self.fx.fx.restart()
        self.fx.inbox = self.fx.fx.inbox
        self.journal = SQLiteWebDeliveryJournal(self.path)
        self.addCleanup(self.journal.close)
        self.manager = TaskDeliveryManager(self.fx.inbox, retry_delay_seconds=0)
        self.fx.gate = TaskPermissionGate(self.fx.inbox, grants=(
            ActionGrant(self.task, "ui_one", ProtectedAction.EXTERNAL_REQUEST),))
        self.fx.capacity = self.fx.scheduler()
        self.fx.adapter = WebCoreTaskAdapter(
            self.fx.inbox, OWNER, self.fx.sessions, self.fx.models,
            self.fx.gate, self.fx.capacity)
        self.dispatcher = WebIdempotentDispatcher(
            self.fx.adapter, self.manager, self.journal)
        self.assertEqual(self.snapshot().state, "ACTIVE")
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.RUNNING)
        self.assertEqual(self.manager.snapshot(self.task, OWNER).status, "active")
        self.assertIsNotNone(self.fx.inbox._store.capacity_reservation(self.task))
        self.assertFalse(self.dispatch().executed)
        self.assertEqual(marker.read_bytes(), b"browser-entered\n")

    def test_journal_permissions_and_foreign_schema_fail_closed(self):
        self.journal.close()
        os.chmod(self.path, 0o644)
        with self.assertRaises(WebDeliveryHold):
            SQLiteWebDeliveryJournal(self.path)
        os.chmod(self.path, 0o600)
        foreign = Path(self.fx.fx.temp.name) / "foreign.sqlite"
        with sqlite3.connect(foreign) as db:
            db.execute("CREATE TABLE other(t TEXT)")
        os.chmod(foreign, 0o600)
        with self.assertRaises(WebDeliveryHold):
            SQLiteWebDeliveryJournal(foreign)

    def test_duplicate_journal_writer_refused(self):
        with self.assertRaises(WebDeliveryHold):
            SQLiteWebDeliveryJournal(self.path)


if __name__ == "__main__":
    unittest.main()
