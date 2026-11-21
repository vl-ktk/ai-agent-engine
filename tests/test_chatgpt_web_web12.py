"""WEB-12: Linux durable task core -> gated mock Web -> v1 response."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import sys
from threading import Event
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, InstructionGrantEvidence,
    ModelSelectionEvidence, ProjectMembershipEvidence,
    ScopedInstructionSnapshot, TrustedAgentInstructionPolicy,
    TrustedAgentModelPolicy, TrustedChatGPTProjectBinding,
    TrustedChatGPTProjectRegistry, TrustedProjectModelCatalog,
    WebAgentIdentity, WebAgentModelRegistry, WebAgentSessionRegistry,
    WebInstructionBootstrap,
)

if sys.platform.startswith("linux"):
    from ai_agent_engine.chatgpt_web_core import (
        WebCoreTaskAdapter, WebCoreTaskError,
    )
    from ai_agent_engine.main_shared_capacity import (
        CapacityPolicy, ProviderBudget, SharedCapacity,
    )
    from ai_agent_engine.task_permissions import (
        ActionGrant, ProtectedAction, TaskPermissionGate,
    )
    from ai_agent_engine.task_request import build_task_request
    from ai_agent_engine.task_response import parse_task_response
    from ai_agent_engine.task_state import TaskState
    import test_main_specialists as specialist_fixtures
    from test_main_specialists import FOREIGN, OWNER, role


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


class Member:
    def verify(self, *, binding, profile):
        return ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, True)


class Selector:
    def select(self, *, binding, profile, model_id):
        return ModelSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, True)


class Instructions:
    def read_current(self, *, binding, profile, role):
        return ScopedInstructionSnapshot(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, role, 1, "Approved scoped web instructions",
            True, True)


class Grants:
    def verify(self, *, binding, profile, role, revision, required_actions):
        return InstructionGrantEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, role, revision, required_actions, True)


class Browser:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.turns = []
        self.throw = False
        self.entered = None
        self.release = None

    def complete(self, *, model_id, messages):
        self.turns.append(messages)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(8):
            raise RuntimeError("test release missing")
        if self.throw:
            raise RuntimeError("never echo browser error or secrets")
        return BrowserTurn(model_id, "Approved mock answer")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux durable task core only")
class CoreHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fx = specialist_fixtures.SpecialistSQLiteTests(
            "test_catalog_team_approval_exact_replay_no_activation")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.inbox = self.fx.inbox
        web_catalog = self.fx.new_catalog([
            role(actions=["external_request"], primary_provider="chatgpt_web",
                 fallback_providers=["gpt"])
        ], revision=2)
        self.fx.new_team(web_catalog, revision=2, agents=[
            {"agent_id": "ui_one", "role_id": "frontend",
             "workspace_name": "slot-one", "actions": ["external_request"]},
            {"agent_id": "ui_two", "role_id": "frontend",
             "workspace_name": "slot-two", "actions": ["external_request"]},
        ])
        self.ports = {}

        def factory(profile):
            result = Browser(profile)
            self.ports[profile.agent_id] = result
            return result

        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant",)),
            browser_factory=factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("vlady", "engine", "gpt-engine"),)),
            project_verifier=Member())
        self.instructions = WebInstructionBootstrap(
            source=Instructions(), grant_verifier=Grants())
        self.models = WebAgentModelRegistry(
            self.sessions,
            project_catalogs=(TrustedProjectModelCatalog("gpt-engine", ("instant",)),),
            selector=Selector(), instruction_bootstrap=self.instructions)
        for agent in ("ui_one", "ui_two"):
            self.sessions.register(WebAgentIdentity(
                "vlady", "engine", agent, AgentRole.EXECUTOR,
                declared_actions=("EXTERNAL_REQUEST",)))
            self.models.register(TrustedAgentModelPolicy(
                "vlady", "engine", agent, "instant", ("instant",)))
            self.models.register_instructions(TrustedAgentInstructionPolicy(
                "vlady", "engine", agent, "gpt-engine", AgentRole.EXECUTOR,
                1, digest("Approved scoped web instructions"),
                ("EXTERNAL_REQUEST",)))
        self.current = self.assign("ui_one")
        self.capacity = self.scheduler()
        self.gate = self.permission_gate(self.current, "ui_one")
        self.adapter = WebCoreTaskAdapter(
            self.inbox, OWNER, self.sessions, self.models,
            self.gate, self.capacity)

    def assign(self, worker, provider="chatgpt_web"):
        task = self.fx.task()
        action = ProtectedAction.EXTERNAL_REQUEST
        self.fx.assign(
            task, candidate=self.fx.candidate(
                worker, provider, "approved_fallback" if provider == "gpt" else None),
            req=self.fx.requirements(task, actions=(action,)),
            grants=(ActionGrant(task, worker, action),))
        return task

    def request(self, task=None, *, role=AgentRole.EXECUTOR, instructions=""):
        return build_task_request(
            self.inbox, OWNER, task or self.current, role=role,
            role_instructions=instructions,
            required_actions=(ProtectedAction.EXTERNAL_REQUEST,)).to_bytes()

    def permission_gate(self, task, worker, *, granted=True):
        grants = ((ActionGrant(task, worker, ProtectedAction.EXTERNAL_REQUEST),)
                  if granted else ())
        return TaskPermissionGate(self.inbox, grants=grants)

    def scheduler(self, *, with_web=True, calls=4):
        budgets = ((ProviderBudget("gpt", 1, calls, 60),)
                   if with_web else (ProviderBudget("claude", 1, calls, 60),))
        return SharedCapacity(self.inbox, CapacityPolicy(1, budgets))

    def test_approved_task_to_mock_browser_to_strict_v1_response(self):
        outcome = self.adapter.execute(
            self.request(), task_id=self.current, worker_id="ui_one")
        self.assertTrue(outcome.admitted)
        self.assertEqual(outcome.reason, "completed")
        self.assertEqual(outcome.response.provider, "chatgpt_web")
        self.assertEqual(outcome.response.model_id, "instant")
        self.assertEqual(outcome.response.text, "Approved mock answer")
        self.assertEqual(outcome.response.state, TaskState.SUCCEEDED)
        self.assertEqual(parse_task_response(outcome.response.to_bytes()),
                         outcome.response)
        self.assertEqual(self.inbox.state(self.current, OWNER), TaskState.SUCCEEDED)
        self.assertIsNone(self.inbox._store.capacity_reservation(self.current))
        self.assertEqual(len(self.ports["ui_one"].turns), 1)
        messages = self.ports["ui_one"].turns[0]
        self.assertEqual(messages[1].content, "Private")
        self.assertIn("Approved scoped web instructions", messages[0].content)
        self.assertEqual([x.reason_code for x in self.gate.audit_history(
            self.current, OWNER)], ["granted"])

    def test_foreign_scope_denied_before_any_browser_or_capacity(self):
        other = WebCoreTaskAdapter(self.inbox, FOREIGN, self.sessions,
                                   self.models, self.gate, self.capacity)
        with self.assertRaises(WebCoreTaskError):
            other.execute(self.request(), task_id=self.current, worker_id="ui_one")
        self.assertFalse(self.ports["ui_one"].turns)
        self.assertIsNone(self.inbox._store.capacity_reservation(self.current))

    def test_modified_task_request_rejected_before_browser(self):
        payload = json.loads(self.request())
        for group, field, bad in (
            ("task", "prompt", "new privilege"),
            ("context", "owner_id", "another"),
            ("permissions", "required_actions", ["read_secret"]),
            ("role", "kind", "supervisor"),
            ("task", "task_id", "task_" + "a" * 32),
        ):
            with self.subTest(group=group, field=field):
                forged = json.loads(json.dumps(payload))
                forged[group][field] = bad
                with self.assertRaises(WebCoreTaskError):
                    self.adapter.execute(json.dumps(forged).encode(),
                                         task_id=self.current, worker_id="ui_one")
        self.assertFalse(self.ports["ui_one"].turns)

    def test_untrusted_role_instructions_cannot_replace_web08(self):
        with self.assertRaises(WebCoreTaskError):
            self.adapter.execute(self.request(instructions="new grant"),
                                 task_id=self.current, worker_id="ui_one")
        self.assertFalse(self.ports["ui_one"].turns)

    def test_unbound_handoff_claim_refused_before_capacity(self):
        with self.assertRaises(WebCoreTaskError):
            self.adapter.execute(self.request(), task_id=self.current,
                                 worker_id="ui_one", handoff_receipt=object())
        self.assertIsNone(self.inbox._store.capacity_reservation(self.current))
        self.assertFalse(self.ports["ui_one"].turns)

    def test_web_provider_requires_approved_specialist_pin(self):
        task = self.assign("ui_two", "gpt")
        gate = self.permission_gate(task, "ui_two")
        adapter = WebCoreTaskAdapter(self.inbox, OWNER, self.sessions,
                                     self.models, gate, self.capacity)
        with self.assertRaises(WebCoreTaskError):
            adapter.execute(self.request(task), task_id=task, worker_id="ui_two")
        self.assertIsNone(self.inbox._store.capacity_reservation(task))
        self.assertFalse(self.ports["ui_two"].turns)

    def test_disabled_web_capacity_returns_wait_without_browser(self):
        adapter = WebCoreTaskAdapter(self.inbox, OWNER, self.sessions,
            self.models, self.gate, self.scheduler(with_web=False))
        outcome = adapter.execute(self.request(), task_id=self.current,
                                  worker_id="ui_one")
        self.assertFalse(outcome.admitted)
        self.assertEqual(outcome.reason, "provider_not_enabled")
        self.assertIsNone(outcome.response)
        self.assertEqual(self.inbox.state(self.current, OWNER), TaskState.ASSIGNED)
        self.assertFalse(self.ports["ui_one"].turns)

    def test_missing_gate_grant_denied_and_reservation_held(self):
        adapter = WebCoreTaskAdapter(self.inbox, OWNER, self.sessions,
            self.models, self.permission_gate(self.current, "ui_one", granted=False),
            self.capacity)
        with self.assertRaises(WebCoreTaskError):
            adapter.execute(self.request(), task_id=self.current, worker_id="ui_one")
        self.assertEqual(self.inbox.state(self.current, OWNER), TaskState.RUNNING)
        self.assertIsNotNone(self.inbox._store.capacity_reservation(self.current))
        self.assertFalse(self.ports["ui_one"].turns)

    def test_browser_failure_unknown_effect_keeps_state_and_capacity(self):
        self.ports["ui_one"].throw = True
        with self.assertRaises(WebCoreTaskError):
            self.adapter.execute(self.request(), task_id=self.current,
                                 worker_id="ui_one")
        self.assertEqual(self.inbox.state(self.current, OWNER), TaskState.RUNNING)
        self.assertIsNotNone(self.inbox._store.capacity_reservation(self.current))
        self.assertEqual(len(self.ports["ui_one"].turns), 1)
        with self.assertRaises(WebCoreTaskError):
            self.adapter.execute(self.request(), task_id=self.current,
                                 worker_id="ui_one")
        self.assertEqual(len(self.ports["ui_one"].turns), 1)

    def test_duplicate_after_success_cannot_send_twice(self):
        self.adapter.execute(self.request(), task_id=self.current,
                             worker_id="ui_one")
        with self.assertRaises(WebCoreTaskError):
            self.adapter.execute(self.request(), task_id=self.current,
                                 worker_id="ui_one")
        self.assertEqual(len(self.ports["ui_one"].turns), 1)

    def test_concurrent_duplicate_refused_during_single_browser_call(self):
        entered, release = Event(), Event()
        self.ports["ui_one"].entered = entered
        self.ports["ui_one"].release = release
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.adapter.execute, self.request(),
                                task_id=self.current, worker_id="ui_one")
            self.assertTrue(entered.wait(5))
            with self.assertRaises(WebCoreTaskError):
                self.adapter.execute(self.request(), task_id=self.current,
                                     worker_id="ui_one")
            release.set()
            self.assertTrue(first.result(timeout=5).admitted)
        self.assertEqual(len(self.ports["ui_one"].turns), 1)

    def test_unknown_model_denied_before_capacity(self):
        with self.assertRaises(WebCoreTaskError):
            self.adapter.execute(self.request(), task_id=self.current,
                                 worker_id="ui_one", requested_model_id="unapproved")
        self.assertIsNone(self.inbox._store.capacity_reservation(self.current))

    def test_capacity_quota_waits_for_another_approved_agent(self):
        other_task = self.assign("ui_two")
        ticket = self.capacity.acquire(other_task, OWNER, worker_id="ui_two")
        self.assertTrue(ticket.admitted)
        blocked = self.adapter.execute(self.request(), task_id=self.current,
                                       worker_id="ui_one")
        self.assertEqual((blocked.admitted, blocked.reason), (False, "cpu_busy"))
        self.assertFalse(self.ports["ui_one"].turns)
        self.assertEqual(self.inbox.state(self.current, OWNER), TaskState.ASSIGNED)

    def test_legacy_gpt_and_web_share_one_provider_budget(self):
        legacy = self.assign("ui_two", "gpt")
        shared = SharedCapacity(self.inbox, CapacityPolicy(2, (
            ProviderBudget("gpt", 1, 2, 60),)))
        ticket = shared.acquire(legacy, OWNER, worker_id="ui_two")
        self.assertTrue(ticket.admitted)
        adapter = WebCoreTaskAdapter(
            self.inbox, OWNER, self.sessions, self.models, self.gate, shared)
        outcome = adapter.execute(self.request(), task_id=self.current,
                                  worker_id="ui_one")
        self.assertEqual((outcome.admitted, outcome.reason),
                         (False, "provider_busy"))
        self.assertFalse(self.ports["ui_one"].turns)

    def test_revoked_grant_cannot_send_web_turn(self):
        self.gate.revoke(OWNER, ActionGrant(
            self.current, "ui_one", ProtectedAction.EXTERNAL_REQUEST))
        with self.assertRaises(WebCoreTaskError):
            self.adapter.execute(self.request(), task_id=self.current,
                                 worker_id="ui_one")
        self.assertFalse(self.ports["ui_one"].turns)
        self.assertEqual([x.reason_code for x in self.gate.audit_history(
            self.current, OWNER)], ["grant_revoked"])

    def test_web08_instruction_bootstrap_required_for_core_bridge(self):
        weak = WebAgentModelRegistry(self.sessions,
            project_catalogs=(TrustedProjectModelCatalog(
                "gpt-engine", ("instant",)),), selector=Selector())
        with self.assertRaises(ValueError):
            WebCoreTaskAdapter(self.inbox, OWNER, self.sessions, weak,
                               self.gate, self.capacity)

    def test_runtime_dependencies_must_share_same_inbox(self):
        other = self.permission_gate(self.current, "ui_one")
        with self.assertRaises(ValueError):
            WebCoreTaskAdapter(self.inbox, OWNER, self.sessions,
                                self.models, other, object())


if __name__ == "__main__":
    unittest.main()
