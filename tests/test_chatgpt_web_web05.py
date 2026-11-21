"""WEB-05: scoped mock project chats, lineage, failures and agent FIFO."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, Lock
import time
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, CreatedProjectChat,
    ProjectChatManager, ProjectMembershipEvidence, TrustedChatGPTProjectBinding,
    TrustedChatGPTProjectRegistry, WebAgentIdentity, WebAgentScopeError,
    WebAgentSessionRegistry,
)


class MembershipVerifier:
    def __init__(self):
        self.verified = True
        self.calls = []

    def verify(self, *, binding, profile):
        self.calls.append(profile.agent_id)
        return ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, self.verified)


class ModelPort:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = []

    def complete(self, *, model_id, messages):
        self.calls.append(messages)
        return BrowserTurn(model_id, "MODEL MOCK")


class ChatPort:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.agent_id = profile.agent_id
        self.calls = []
        self.mutator = None
        self.block_first = None
        self.entered = None

    def create_chat(self, *, chatgpt_project_id, predecessor_id):
        self.calls.append((chatgpt_project_id, predecessor_id))
        if len(self.calls) == 1 and self.entered is not None:
            self.entered.set()
        if len(self.calls) == 1 and self.block_first is not None:
            if not self.block_first.wait(timeout=12):
                raise RuntimeError("test-only timeout")
        created = CreatedProjectChat(
            f"chat-{self.agent_id}-{len(self.calls)}", chatgpt_project_id,
            self.profile_key, self.mailbox_key)
        if self.mutator is not None:
            return self.mutator(created)
        return created


class ChatManagerTests(unittest.TestCase):
    def setUp(self):
        self.model_ports = {}
        self.chat_ports = {}
        self.verifier = MembershipVerifier()
        self.projects = TrustedChatGPTProjectRegistry((
            TrustedChatGPTProjectBinding("owner-a", "internal-one", "gpt-one"),
            TrustedChatGPTProjectBinding("owner-a", "internal-two", "gpt-two"),
        ))

        def model_factory(profile):
            port = ModelPort(profile)
            self.model_ports[profile.agent_id] = port
            return port

        def chat_factory(profile):
            port = ChatPort(profile)
            self.chat_ports[profile.agent_id] = port
            return port

        self.chat_factory = chat_factory
        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("synthetic",)),
            browser_factory=model_factory, project_registry=self.projects,
            project_verifier=self.verifier)
        self.manager = ProjectChatManager(self.sessions,
                                          chat_port_factory=chat_factory)

    def register(self, agent="agent-one", project="internal-one"):
        return self.sessions.register(WebAgentIdentity(
            "owner-a", project, agent, AgentRole.EXECUTOR))

    def scope(self, agent="agent-one", project="internal-one", owner="owner-a"):
        return dict(owner_id=owner, project_id=project, agent_id=agent)

    def test_initial_chat_is_project_scoped_with_no_root_fallback(self):
        self.register()
        record = self.manager.create_initial(**self.scope())
        self.assertEqual(record.conversation_id, "chat-agent-one-1")
        self.assertEqual(record.chatgpt_project_id, "gpt-one")
        self.assertIsNone(record.predecessor_id)
        self.assertIsNone(record.next_id)
        self.assertEqual(self.chat_ports["agent-one"].calls, [("gpt-one", None)])
        self.assertEqual(self.manager.active(**self.scope()), record)
        self.assertEqual(self.manager.history(**self.scope()), (record,))
        self.assertEqual(self.model_ports["agent-one"].calls, [])

    def test_rotations_link_predecessor_and_next_without_rebinding(self):
        self.register()
        first = self.manager.create_initial(**self.scope())
        second = self.manager.rotate(**self.scope())
        third = self.manager.rotate(**self.scope())
        chain = self.manager.history(**self.scope())
        self.assertEqual(len(chain), 3)
        self.assertEqual(chain[0].predecessor_id, None)
        self.assertEqual(chain[0].next_id, second.conversation_id)
        self.assertEqual(chain[1].predecessor_id, first.conversation_id)
        self.assertEqual(chain[1].next_id, third.conversation_id)
        self.assertEqual(chain[2].predecessor_id, second.conversation_id)
        self.assertIsNone(chain[2].next_id)
        self.assertEqual(self.manager.active(**self.scope()), third)
        self.assertEqual(self.chat_ports["agent-one"].calls,
                         [("gpt-one", None), ("gpt-one", first.conversation_id),
                          ("gpt-one", second.conversation_id)])

    def test_two_agents_in_different_projects_have_separate_chains(self):
        self.register()
        self.register("agent-two", "internal-two")
        self.manager.create_initial(**self.scope())
        self.manager.create_initial(**self.scope("agent-two", "internal-two"))
        self.manager.rotate(**self.scope())
        self.assertEqual(len(self.manager.history(**self.scope())), 2)
        self.assertEqual(len(self.manager.history(
            **self.scope("agent-two", "internal-two"))), 1)
        self.assertEqual(self.chat_ports["agent-two"].calls, [("gpt-two", None)])

    def test_wrong_owner_project_or_agent_can_never_read_or_create(self):
        self.register()
        self.manager.create_initial(**self.scope())
        for bad in (self.scope(owner="owner-b"),
                    self.scope(project="internal-two"),
                    self.scope(agent="agent-foreign")):
            with self.subTest(bad=bad):
                for operation in (self.manager.active, self.manager.history,
                                  self.manager.rotate, self.manager.create_initial):
                    with self.assertRaises(WebAgentScopeError):
                        operation(**bad)
        self.assertEqual(len(self.chat_ports["agent-one"].calls), 1)

    def test_no_unbound_or_unverified_project_chat_creation(self):
        self.register()
        self.verifier.verified = False
        with self.assertRaises(WebAgentScopeError):
            self.manager.create_initial(**self.scope())
        self.assertEqual(self.chat_ports, {})
        self.assertEqual(self.manager.history(**self.scope()), ())
        self.verifier.verified = True
        self.manager.create_initial(**self.scope())
        self.verifier.verified = False
        with self.assertRaises(WebAgentScopeError):
            self.manager.rotate(**self.scope())
        self.assertEqual(len(self.manager.history(**self.scope())), 1)
        self.assertEqual(len(self.chat_ports["agent-one"].calls), 1)

    def test_legacy_registry_without_project_gate_cannot_create_chat(self):
        legacy = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("synthetic",)),
            browser_factory=lambda p: ModelPort(p))
        legacy.register(WebAgentIdentity("owner-a", "internal-one", "agent-one",
                                         AgentRole.EXECUTOR))
        manager = ProjectChatManager(legacy, chat_port_factory=self.chat_factory)
        with self.assertRaises(WebAgentScopeError):
            manager.create_initial(**self.scope())
        self.assertEqual(self.chat_ports, {})

    def test_root_chat_claim_is_refused_without_changing_active(self):
        self.register()
        self.manager.create_initial(**self.scope())
        port = self.chat_ports["agent-one"]
        port.mutator = lambda chat: replace(chat, chatgpt_project_id="")
        with self.assertRaises(WebAgentScopeError):
            self.manager.rotate(**self.scope())
        self.assertEqual(self.manager.active(**self.scope()).conversation_id,
                         "chat-agent-one-1")
        self.assertIsNone(self.manager.active(**self.scope()).next_id)

    def test_foreign_project_and_profile_claims_refused(self):
        self.register()
        self.manager.create_initial(**self.scope())
        port = self.chat_ports["agent-one"]
        for field, value in (("chatgpt_project_id", "gpt-two"),
                             ("profile_key", "profile-other"),
                             ("mailbox_key", "mailbox-other")):
            port.mutator = lambda chat, f=field, v=value: replace(chat, **{f: v})
            with self.subTest(field=field), self.assertRaises(WebAgentScopeError):
                self.manager.rotate(**self.scope())
        self.assertEqual(len(self.manager.history(**self.scope())), 1)

    def test_malformed_id_or_result_cannot_be_promoted(self):
        self.register()
        self.manager.create_initial(**self.scope())
        port = self.chat_ports["agent-one"]
        for mutate in (lambda c: replace(c, conversation_id=""),
                       lambda c: replace(c, conversation_id="../escape"),
                       lambda c: replace(c, conversation_id="with spaces"),
                       lambda c: replace(c, conversation_id=None),
                       lambda c: None, lambda c: {"chatgpt_project_id": "gpt-one"}):
            port.mutator = mutate
            with self.subTest(mutate=mutate), self.assertRaises(WebAgentScopeError):
                self.manager.rotate(**self.scope())
        self.assertEqual(len(self.manager.history(**self.scope())), 1)

    def test_duplicate_conversation_id_rejected_across_agents(self):
        self.register()
        self.register("agent-two", "internal-two")
        first = self.manager.create_initial(**self.scope())

        def override(profile):
            port = ChatPort(profile)
            port.mutator = lambda c: replace(c, conversation_id=first.conversation_id)
            self.chat_ports[profile.agent_id] = port
            return port

        self.manager._factory = override
        with self.assertRaises(WebAgentScopeError):
            self.manager.create_initial(**self.scope("agent-two", "internal-two"))
        self.assertEqual(self.manager.history(**self.scope("agent-two", "internal-two")), ())
        self.assertEqual(len(self.manager.history(**self.scope())), 1)

    def test_duplicate_initial_and_rotate_without_initial_do_not_call_port(self):
        self.register()
        with self.assertRaises(WebAgentScopeError):
            self.manager.rotate(**self.scope())
        self.assertEqual(self.chat_ports, {})
        self.manager.create_initial(**self.scope())
        with self.assertRaises(WebAgentScopeError):
            self.manager.create_initial(**self.scope())
        self.assertEqual(len(self.chat_ports["agent-one"].calls), 1)

    def test_bad_factory_and_port_reuse_fail_closed(self):
        self.register()
        self.register("agent-two", "internal-two")
        shared = []

        def factory(profile):
            if not shared:
                shared.append(ChatPort(profile))
            return shared[0]

        manager = ProjectChatManager(self.sessions, chat_port_factory=factory)
        manager.create_initial(**self.scope())
        with self.assertRaises(WebAgentScopeError):
            manager.create_initial(**self.scope("agent-two", "internal-two"))
        self.assertEqual(manager.history(**self.scope("agent-two", "internal-two")), ())
        self.assertEqual(len(shared[0].calls), 1)

    def test_wrong_port_profile_claim_refused_without_send(self):
        self.register()

        def wrong(profile):
            port = ChatPort(profile)
            port.profile_key = "not-the-expected-agent-profile"
            return port

        manager = ProjectChatManager(self.sessions, chat_port_factory=wrong)
        with self.assertRaises(WebAgentScopeError):
            manager.create_initial(**self.scope())
        self.assertEqual(manager.history(**self.scope()), ())

    def test_port_error_is_sanitized_without_implicit_retry(self):
        self.register()
        self.manager.create_initial(**self.scope())
        port = self.chat_ports["agent-one"]

        def fail(_):
            raise RuntimeError("mock private secret")

        port.mutator = fail
        with self.assertRaises(WebAgentScopeError) as error:
            self.manager.rotate(**self.scope())
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(len(port.calls), 2)
        self.assertEqual(len(self.manager.history(**self.scope())), 1)

    def test_verified_creation_and_model_turn_share_agent_fifo(self):
        self.register()
        entered, release = Event(), Event()

        def port_factory(profile):
            port = ChatPort(profile)
            port.entered = entered
            port.block_first = release
            self.chat_ports[profile.agent_id] = port
            return port

        manager = ProjectChatManager(self.sessions, chat_port_factory=port_factory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            creating = pool.submit(manager.create_initial, **self.scope())
            self.assertTrue(entered.wait(timeout=5))
            running = pool.submit(self.sessions.run, **self.scope(),
                                  model_id="synthetic", prompt="work")
            time.sleep(0.04)
            self.assertEqual(self.model_ports["agent-one"].calls, [])
            release.set()
            self.assertEqual(creating.result(timeout=6).chatgpt_project_id, "gpt-one")
            self.assertEqual(running.result(timeout=6).answer, "MODEL MOCK")
        self.assertEqual(len(self.model_ports["agent-one"].calls), 1)

    def test_different_agents_do_not_share_creation_lock(self):
        self.register()
        self.register("agent-two", "internal-two")
        entered, release = Event(), Event()

        def factory(profile):
            port = ChatPort(profile)
            if profile.agent_id == "agent-one":
                port.entered, port.block_first = entered, release
            self.chat_ports[profile.agent_id] = port
            return port

        manager = ProjectChatManager(self.sessions, chat_port_factory=factory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(manager.create_initial, **self.scope())
            self.assertTrue(entered.wait(timeout=5))
            other = pool.submit(manager.create_initial,
                                **self.scope("agent-two", "internal-two"))
            self.assertEqual(other.result(timeout=4).chatgpt_project_id, "gpt-two")
            release.set()
            self.assertEqual(first.result(timeout=6).chatgpt_project_id, "gpt-one")

    def test_constructor_rejects_untrusted_inputs(self):
        for sessions, factory in ((object(), self.chat_factory),
                                  (self.sessions, None),
                                  (self.sessions, object())):
            with self.subTest(sessions=sessions, factory=factory):
                with self.assertRaises((TypeError, ValueError)):
                    ProjectChatManager(sessions, chat_port_factory=factory)


if __name__ == "__main__":
    unittest.main()
