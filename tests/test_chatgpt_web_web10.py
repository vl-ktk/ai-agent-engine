"""WEB-10: idempotent offline Project rotation and SQLite crash/retry."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, ContextUsageEvidence,
    CreatedProjectChat, ModelSelectionEvidence, ProjectChatManager,
    ProjectMembershipEvidence, SQLiteWebRotationJournal,
    TrustedAgentContextPolicy, TrustedAgentModelPolicy,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
    TrustedProjectModelCatalog, WebAgentIdentity, WebAgentModelRegistry,
    WebAgentSessionRegistry, WebAutomaticChatRotator, WebChatRotationError,
    WebContextMonitor, WebContextPressureError,
)


class Membership:
    def __init__(self):
        self.allow = True

    def verify(self, *, binding, profile):
        return ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, self.allow)


class ModelSelector:
    def select(self, *, binding, profile, model_id):
        return ModelSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, True)


class Browser:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = []

    def complete(self, *, model_id, messages):
        self.calls.append(messages)
        return BrowserTurn(model_id, "ACK:" + messages[-1].content)


class Usage:
    def __init__(self):
        self.used = 7600

    def read_current(self, *, binding, profile, conversation_id):
        return ContextUsageEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, conversation_id, "exact", self.used,
            self.used, True, True)


class DurableMockChatPort:
    """Offline browser replacement: operation_id is UNIQUE on the fake server."""

    def __init__(self, profile, db_path):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.agent_id = profile.agent_id
        self.db_path = db_path
        self.create_calls = 0
        self.reconcile_calls = 0
        self.fail_after_create_once = False
        self.mutator = None
        with closing(sqlite3.connect(self.db_path)) as db:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS fake_web_chat_operations (
                    rotation TEXT PRIMARY KEY, project TEXT NOT NULL,
                    predecessor TEXT NOT NULL, task TEXT NOT NULL,
                    conversation TEXT NOT NULL UNIQUE)""")

    def create_chat(self, *, chatgpt_project_id, predecessor_id):
        # Initial chat only, never used for WEB-10 replay.
        return CreatedProjectChat(
            f"init-{self.agent_id}", chatgpt_project_id,
            self.profile_key, self.mailbox_key)

    def create_or_get_chat(self, *, chatgpt_project_id, predecessor_id,
                           task_id, rotation_id):
        self.reconcile_calls += 1
        with closing(sqlite3.connect(self.db_path, timeout=8)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("""SELECT project,predecessor,task,conversation
                    FROM fake_web_chat_operations WHERE rotation=?""",
                    (rotation_id,)).fetchone()
                if row is None:
                    conversation_id = "rot-" + rotation_id[:32]
                    db.execute("INSERT INTO fake_web_chat_operations VALUES (?,?,?,?,?)",
                               (rotation_id, chatgpt_project_id, predecessor_id,
                                task_id, conversation_id))
                    self.create_calls += 1
                else:
                    if row[:3] != (chatgpt_project_id, predecessor_id, task_id):
                        raise RuntimeError("mock idempotency scope mismatch")
                    conversation_id = row[3]
        if self.fail_after_create_once:
            self.fail_after_create_once = False
            raise RuntimeError("test-only driver crash after remote creation")
        proof = CreatedProjectChat(conversation_id, chatgpt_project_id,
                                   self.profile_key, self.mailbox_key)
        return self.mutator(proof) if self.mutator else proof


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "web10-offline.sqlite")
        self.make_runtime(initial=True)

    def scope(self, agent="agent-one", project="project-one", owner="owner"):
        return dict(owner_id=owner, project_id=project, agent_id=agent)

    def make_runtime(self, *, initial=False):
        self.membership = Membership()
        self.usage = Usage()
        self.browser_ports = {}
        self.chat_ports = {}

        def browser_factory(profile):
            browser = Browser(profile)
            self.browser_ports[profile.agent_id] = browser
            return browser

        def chat_factory(profile):
            port = DurableMockChatPort(profile, self.path)
            self.chat_ports[profile.agent_id] = port
            return port

        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant",)),
            browser_factory=browser_factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("owner", "project-one", "gpt-one"),
                TrustedChatGPTProjectBinding("owner", "project-two", "gpt-two"))),
            project_verifier=self.membership)
        self.chats = ProjectChatManager(self.sessions,
                                        chat_port_factory=chat_factory)
        self.monitor = WebContextMonitor(self.sessions, self.chats,
                                         usage_source=self.usage)
        self.models = WebAgentModelRegistry(
            self.sessions, project_catalogs=(
                TrustedProjectModelCatalog("gpt-one", ("instant",)),
                TrustedProjectModelCatalog("gpt-two", ("instant",))),
            selector=ModelSelector(), context_monitor=self.monitor)
        self.journal = SQLiteWebRotationJournal(self.path)
        self.rotator = WebAutomaticChatRotator(
            self.sessions, self.chats, self.journal, self.monitor)
        self.register("agent-one", "project-one", "gpt-one")
        if initial:
            self.chats.create_initial(**self.scope())
            self.rotator.track_initial(**self.scope())

    def register(self, agent, project, gpt_project):
        self.sessions.register(WebAgentIdentity(
            "owner", project, agent, AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner", project, agent, "instant", ("instant",)))
        self.models.register_context(TrustedAgentContextPolicy(
            "owner", project, agent, gpt_project, 10_000,
            response_reserve_tokens=0))

    def trigger(self, *, scope=None):
        scope = self.scope() if scope is None else scope
        with self.assertRaises(WebContextPressureError):
            self.models.run(**scope, prompt="job")
        decision = self.monitor.last_decision(**scope)
        self.assertEqual(decision.action, "ROTATE")
        return decision

    def rotate(self, decision, task="task-42", checkpoint="checkpoint-9"):
        return self.rotator.rotate(decision=decision, task_id=task,
                                   checkpoint_id=checkpoint)

    def created_rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT rotation,project,predecessor,task,conversation FROM fake_web_chat_operations").fetchall()

    def test_full_pressure_handoff_and_task_continuation_same_project(self):
        decision = self.trigger()
        self.assertFalse(self.browser_ports["agent-one"].calls)
        receipt = self.rotator.rotate_on_pressure(
            **self.scope(), task_id="task-42", checkpoint_id="checkpoint-9")
        self.assertEqual((receipt.agent_id, receipt.task_id, receipt.checkpoint_id),
                         ("agent-one", "task-42", "checkpoint-9"))
        self.assertEqual(receipt.predecessor_id, "init-agent-one")
        self.assertEqual(receipt.chatgpt_project_id, "gpt-one")
        chain = self.chats.history(**self.scope())
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].next_id, receipt.conversation_id)
        self.assertEqual(chain[1].predecessor_id, "init-agent-one")
        self.assertEqual(chain[1].task_id, "task-42")
        self.assertEqual(chain[1].rotation_id, receipt.rotation_id)
        self.assertEqual(len(self.created_rows()), 1)
        # The pre-rotation request was never sent. Task remains the same ID;
        # the next mock send is explicit, not an implicit retry.
        self.usage.used = 0
        self.assertEqual(self.models.run(**self.scope(), prompt="job").answer,
                         "ACK:job")
        self.assertEqual(len(self.browser_ports["agent-one"].calls), 1)
        self.assertEqual(self.chats.active(**self.scope()).conversation_id,
                         receipt.conversation_id)

    def test_repeated_delivery_of_same_task_returns_identical_receipt(self):
        decision = self.trigger()
        first = self.rotate(decision)
        for _ in range(4):
            self.assertEqual(self.rotate(decision), first)
        self.assertEqual(self.chat_ports["agent-one"].create_calls, 1)
        self.assertEqual(self.chat_ports["agent-one"].reconcile_calls, 1)
        self.assertEqual(len(self.chats.history(**self.scope())), 2)
        self.assertEqual(len(self.created_rows()), 1)

    def test_concurrent_double_delivery_only_creates_once(self):
        decision = self.trigger()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.rotate(decision), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.created_rows()), 1)
        self.assertEqual(len(self.chats.history(**self.scope())), 2)

    def test_crash_after_mock_side_effect_reconciles_on_fresh_manager(self):
        decision = self.trigger()
        # First call creates the external mock chat but crashes before proof.
        self.chat_ports["agent-one"] = self.chats._port(
            self.sessions.profile(**self.scope()))
        self.chat_ports["agent-one"].fail_after_create_once = True
        with self.assertRaises(Exception) as error:
            self.rotate(decision)
        self.assertNotIn("test-only", str(error.exception))
        self.assertEqual(len(self.chats.history(**self.scope())), 1)
        self.assertEqual(len(self.created_rows()), 1)
        self.assertEqual(len(self.journal.history(**self.scope())), 1)

        # Recreate sessions, manager, monitor and journal from the same file.
        # Durable PREPARED state permits the same operation with no fresh turn.
        self.make_runtime(initial=False)
        self.assertEqual(len(self.rotator.restore_agent(**self.scope())), 1)
        receipt = self.rotate(decision)
        self.assertEqual(len(self.created_rows()), 1)
        self.assertEqual(len(self.chats.history(**self.scope())), 2)
        self.assertEqual(self.chat_ports["agent-one"].create_calls, 0)
        self.assertEqual(self.chat_ports["agent-one"].reconcile_calls, 1)
        self.assertEqual(receipt.task_id, "task-42")

    def test_real_process_exit_after_remote_mock_create_recovers_without_duplicate(self):
        decision = self.trigger()
        worker = """
import os, sys
from tests.test_chatgpt_web_web10 import RotationTests
t = RotationTests('test_full_pressure_handoff_and_task_continuation_same_project')
t.path = sys.argv[1]
t.make_runtime(initial=False)
t.rotator.restore_agent(**t.scope())
pressure = t.trigger()
port = t.chats._port(t.sessions.profile(**t.scope()))
original = port.create_or_get_chat
def crash_after_creation(**kwargs):
    original(**kwargs)
    os._exit(37)
port.create_or_get_chat = crash_after_creation
t.rotate(pressure)
os._exit(99)
"""
        child = subprocess.run(
            [sys.executable, "-c", worker, self.path],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=12, check=False)
        self.assertEqual(child.returncode, 37, child.stderr[-1000:])
        self.assertEqual(len(self.created_rows()), 1)
        self.assertEqual(len(self.journal.history(**self.scope())), 1)
        # Simulates a new server process reading exactly the same disk state.
        self.make_runtime(initial=False)
        self.rotator.restore_agent(**self.scope())
        receipt = self.rotate(decision)
        self.assertEqual(receipt.predecessor_id, "init-agent-one")
        self.assertEqual(len(self.created_rows()), 1)
        self.assertEqual(len(self.journal.history(**self.scope())), 2)

    def test_crash_between_manager_promotion_and_sqlite_commit_replays(self):
        decision = self.trigger()
        original = self.journal.commit

        def crash_once(_):
            raise RuntimeError("crash before journal commit")

        self.journal.commit = crash_once
        with self.assertRaisesRegex(WebChatRotationError,
                                    "rotation journal commit unverified"):
            self.rotate(decision)
        self.journal.commit = original
        self.assertEqual(len(self.chats.history(**self.scope())), 2)
        self.assertEqual(len(self.journal.history(**self.scope())), 1)
        self.make_runtime(initial=False)
        self.rotator.restore_agent(**self.scope())
        receipt = self.rotate(decision)
        self.assertEqual(len(self.created_rows()), 1)
        self.assertEqual(len(self.journal.history(**self.scope())), 2)
        self.assertEqual(receipt.task_id, "task-42")

    def test_committed_journal_restores_chain_without_external_create(self):
        decision = self.trigger()
        first = self.rotate(decision)
        self.make_runtime(initial=False)
        restored = self.rotator.restore_agent(**self.scope())
        self.assertEqual(len(restored), 2)
        self.assertEqual(restored[0].next_id, first.conversation_id)
        self.assertEqual(restored[1].rotation_id, first.rotation_id)
        self.assertEqual(self.rotate(decision), first)
        self.assertEqual(len(self.created_rows()), 1)
        self.assertEqual(self.chat_ports, {})  # no fake external create on retry

    def test_stale_or_forged_pressure_decision_does_not_create_chat(self):
        decision = self.trigger()
        for bad in (replace(decision, action="WARN"),
                    replace(decision, action="ROTATE"),
                    replace(decision, owner_id="foreign"),
                    replace(decision, project_id="project-two"),
                    replace(decision, conversation_id="not-current")):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.rotate(bad)
        self.assertEqual(self.created_rows(), [])
        self.assertEqual(len(self.chats.history(**self.scope())), 1)

    def test_auto_trigger_requires_verified_rotation_not_normal_turn(self):
        self.usage.used = 0
        self.assertEqual(self.models.run(**self.scope(), prompt="safe").status,
                         "done")
        with self.assertRaises(WebChatRotationError):
            self.rotator.rotate_on_pressure(
                **self.scope(), task_id="task-safe", checkpoint_id="cp-safe")
        self.assertEqual(self.created_rows(), [])

    def test_emergency_threshold_rotates_only_with_verified_checkpoint(self):
        self.usage.used = 9100
        with self.assertRaises(WebContextPressureError):
            self.models.run(**self.scope(), prompt="danger")
        self.assertEqual(self.monitor.last_decision(**self.scope()).action,
                         "EMERGENCY")
        receipt = self.rotator.rotate_on_pressure(
            **self.scope(), task_id="critical-task", checkpoint_id="safe-cp")
        self.assertEqual(receipt.task_id, "critical-task")
        self.assertEqual(len(self.created_rows()), 1)

    def test_task_and_checkpoint_replay_conflicts_refused(self):
        decision = self.trigger()
        self.rotate(decision)
        for task, checkpoint in (("task-42", "different"),
                                 ("another-task", "checkpoint-9")):
            with self.subTest(task=task), self.assertRaises(WebChatRotationError):
                self.rotate(decision, task=task, checkpoint=checkpoint)
        self.assertEqual(len(self.created_rows()), 1)

    def test_project_membership_failure_blocks_new_chat(self):
        decision = self.trigger()
        self.membership.allow = False
        with self.assertRaises(Exception):
            self.rotate(decision)
        self.assertEqual(self.created_rows(), [])

    def test_non_idempotent_port_refused_not_silently_retried(self):
        decision = self.trigger()
        port = self.chats._port(self.sessions.profile(**self.scope()))
        port.create_or_get_chat = None
        with self.assertRaises(Exception):
            self.rotate(decision)
        self.assertEqual(self.created_rows(), [])

    def test_wrong_port_scope_refused_no_journal_commit(self):
        decision = self.trigger()
        port = self.chats._port(self.sessions.profile(**self.scope()))
        port.mutator = lambda c: replace(c, chatgpt_project_id="gpt-two")
        with self.assertRaises(Exception):
            self.rotate(decision)
        self.assertEqual(len(self.journal.history(**self.scope())), 1)
        self.assertEqual(len(self.chats.history(**self.scope())), 1)

    def test_initial_and_restore_scope_integrity(self):
        self.rotator.track_initial(**self.scope())
        with self.assertRaises(Exception):
            self.rotator.restore_agent(**self.scope())
        for scope in (self.scope(owner="foreign"),
                      self.scope(project="project-two")):
            with self.assertRaises(Exception):
                self.rotator.track_initial(**scope)

    def test_two_agents_remain_isolated_during_rotation(self):
        self.register("agent-two", "project-two", "gpt-two")
        self.chats.create_initial(**self.scope("agent-two", "project-two"))
        self.rotator.track_initial(**self.scope("agent-two", "project-two"))
        decision_one = self.trigger()
        decision_two = self.trigger(scope=self.scope("agent-two", "project-two"))
        first = self.rotate(decision_one, task="task-one")
        second = self.rotate(decision_two, task="task-two")
        self.assertNotEqual(first.conversation_id, second.conversation_id)
        self.assertEqual(first.chatgpt_project_id, "gpt-one")
        self.assertEqual(second.chatgpt_project_id, "gpt-two")
        self.assertEqual(len(self.created_rows()), 2)

    def test_invalid_journal_path_and_unverified_dependency(self):
        for path in ("", ":memory:", None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                SQLiteWebRotationJournal(path)
        with self.assertRaises(ValueError):
            WebAutomaticChatRotator(self.sessions, self.chats,
                                    self.journal, object())


if __name__ == "__main__":
    unittest.main()
