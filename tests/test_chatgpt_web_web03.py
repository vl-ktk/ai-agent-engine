"""WEB-03: two-agent offline isolation; never instantiate a real browser."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import time
import unittest

from ai_agent_engine import AgentRole, BrowserTurn, ChatGPTWebConfig
from ai_agent_engine.chatgpt_web import ChatGPTWebUnavailable
from ai_agent_engine.chatgpt_web_sessions import (
    WebAgentIdentity, WebAgentScopeError, WebAgentSessionRegistry,
)


def identity(*, owner="owner-a", project="project-one", agent="agent-one",
             role=AgentRole.EXECUTOR, instructions="Only this agent",
             actions=()):
    return WebAgentIdentity(owner, project, agent, role, instructions, actions)


def run_agent(registry, *, owner="owner-a", project="project-one",
              agent="agent-one", prompt="work"):
    return registry.run(owner_id=owner, project_id=project, agent_id=agent,
                        model_id="fake-model", prompt=prompt)


class RecordingPort:
    def __init__(self, profile, *, block_first=None, entered=None, error_first=False):
        self.profile = profile
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = []
        self._lock = Lock()
        self.block_first = block_first
        self.entered = entered
        self.error_first = error_first

    def complete(self, *, model_id, messages):
        with self._lock:
            self.calls.append((model_id, messages))
            index = len(self.calls)
        if index == 1 and self.entered is not None:
            self.entered.set()
        if index == 1 and self.block_first is not None:
            if not self.block_first.wait(timeout=12):
                raise RuntimeError("test-only first turn timed out")
        if index == 1 and self.error_first:
            raise RuntimeError("secret mock failure")
        return BrowserTurn(model_id, self.profile.agent_id + ":" + messages[-1].content)


class AgentIdentityTests(unittest.TestCase):
    def test_scope_and_declaration_validation(self):
        bad = (
            {"owner": "../owner"}, {"project": ""},
            {"agent": "agent with spaces"}, {"role": "executor"},
            {"instructions": "x" * 8001}, {"actions": ["READ_ONLY"]},
            {"actions": ("READ_ONLY", "READ_ONLY")},
            {"actions": ("unsafe.action",)},
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                identity(**kwargs)

    def test_stable_opaque_profile_and_mailbox_namespaces(self):
        def make():
            return WebAgentSessionRegistry(
                ChatGPTWebConfig(enabled=True, models=("fake-model",)),
                browser_factory=lambda profile: RecordingPort(profile))

        one = make()
        first = one.register(identity())
        second = one.register(identity(agent="agent-two"))
        third = one.register(identity(owner="owner-b", agent="agent-three"))
        fourth = one.register(identity(project="project-two", agent="agent-four"))
        other_registry = make()
        same = other_registry.register(identity())
        self.assertEqual(first, same)
        self.assertTrue(first.profile_key.startswith("profile-"))
        self.assertTrue(first.mailbox_key.startswith("mailbox-"))
        self.assertNotEqual(first.profile_key, first.mailbox_key)
        self.assertEqual(len({x.profile_key for x in (first, second, third, fourth)}), 4)
        self.assertEqual(len({x.mailbox_key for x in (first, second, third, fourth)}), 4)
        self.assertEqual(first.agent_id, "agent-one")

    def test_declared_actions_are_private_metadata_not_tool_grants(self):
        registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)),
            browser_factory=lambda profile: RecordingPort(profile))
        registry.register(identity(actions=("READ_REPORT",)))
        registry.register(identity(agent="agent-two", actions=("WRITE_WORKSPACE",)))
        self.assertEqual(registry.identity(owner_id="owner-a", project_id="project-one",
                                           agent_id="agent-one").declared_actions,
                         ("READ_REPORT",))
        self.assertEqual(registry.identity(owner_id="owner-a", project_id="project-one",
                                           agent_id="agent-two").declared_actions,
                         ("WRITE_WORKSPACE",))
        with self.assertRaises(WebAgentScopeError):
            registry.identity(owner_id="owner-a", project_id="project-two",
                              agent_id="agent-two")


class AgentRegistryTests(unittest.TestCase):
    def setUp(self):
        self.ports = {}

        def factory(profile):
            port = RecordingPort(profile)
            self.ports[profile.agent_id] = port
            return port

        self.registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)),
            browser_factory=factory)

    def test_two_agents_have_distinct_profiles_sessions_and_prompts(self):
        one = self.registry.register(identity(instructions="Confidential one"))
        two = self.registry.register(identity(agent="agent-two", project="project-two",
                                              role=AgentRole.SUPERVISOR,
                                              instructions="Confidential two"))
        first = run_agent(self.registry, prompt="task-one")
        second = run_agent(self.registry, agent="agent-two", project="project-two",
                           prompt="task-two")
        self.assertEqual(first.answer, "agent-one:task-one")
        self.assertEqual(second.answer, "agent-two:task-two")
        self.assertEqual(first.audit[0].project_id, "project-one")
        self.assertEqual(second.audit[0].project_id, "project-two")
        self.assertEqual(first.audit[0].agent_id, "agent-one")
        self.assertEqual(second.audit[0].agent_id, "agent-two")
        self.assertNotEqual(one.profile_key, two.profile_key)
        self.assertIsNot(self.ports["agent-one"], self.ports["agent-two"])
        system_one = self.ports["agent-one"].calls[0][1][0].content
        system_two = self.ports["agent-two"].calls[0][1][0].content
        self.assertIn("Confidential one", system_one)
        self.assertNotIn("Confidential two", system_one)
        self.assertIn("Confidential two", system_two)
        self.assertNotIn("Confidential one", system_two)
        self.assertIn("Project: project-two", system_two)

    def test_repeated_turns_use_same_profile_but_new_single_task_context(self):
        profile = self.registry.register(identity())
        for prompt in ("first", "second"):
            self.assertEqual(run_agent(self.registry, prompt=prompt).answer,
                             "agent-one:" + prompt)
        self.assertEqual(len(self.ports["agent-one"].calls), 2)
        self.assertEqual(self.registry.profile(owner_id="owner-a", project_id="project-one",
                                               agent_id="agent-one"), profile)
        self.assertEqual(self.registry.pending(owner_id="owner-a", project_id="project-one",
                                               agent_id="agent-one"), 0)

    def test_wrong_owner_project_or_agent_cannot_use_or_inspect_other_slot(self):
        self.registry.register(identity())
        bad = (("owner-b", "project-one", "agent-one"),
               ("owner-a", "project-two", "agent-one"),
               ("owner-a", "project-one", "agent-two"),
               ("owner-a", "../project", "agent-one"))
        for owner, project, agent in bad:
            with self.subTest(key=(owner, project, agent)):
                with self.assertRaises(WebAgentScopeError):
                    run_agent(self.registry, owner=owner, project=project,
                              agent=agent, prompt="foreign")
                with self.assertRaises(WebAgentScopeError):
                    self.registry.profile(owner_id=owner, project_id=project,
                                          agent_id=agent)
                with self.assertRaises(WebAgentScopeError):
                    self.registry.pending(owner_id=owner, project_id=project,
                                          agent_id=agent)
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_rebinding_agent_id_is_denied_before_creating_another_browser(self):
        self.registry.register(identity())
        with self.assertRaises(WebAgentScopeError):
            self.registry.register(identity(owner="owner-other"))
        with self.assertRaises(WebAgentScopeError):
            self.registry.register(identity())
        self.assertEqual(len(self.ports), 1)

    def test_wrong_model_or_invalid_prompt_never_reaches_browser(self):
        self.registry.register(identity())
        with self.assertRaises(ChatGPTWebUnavailable):
            self.registry.run(owner_id="owner-a", project_id="project-one",
                              agent_id="agent-one", model_id="other-model",
                              prompt="hello")
        with self.assertRaises(ValueError):
            run_agent(self.registry, prompt=" ")
        self.assertEqual(self.ports["agent-one"].calls, [])
        self.assertEqual(self.registry.pending(owner_id="owner-a", project_id="project-one",
                                               agent_id="agent-one"), 0)

    def test_same_port_cannot_be_registered_for_two_agents(self):
        shared = []

        def factory(profile):
            if not shared:
                shared.append(RecordingPort(profile))
            return shared[0]

        registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)),
            browser_factory=factory)
        registry.register(identity())
        with self.assertRaises(ChatGPTWebUnavailable):
            registry.register(identity(agent="agent-two"))
        self.assertEqual(run_agent(registry).answer, "agent-one:work")

    def test_wrong_profile_claim_is_denied_before_any_turn(self):
        def factory(profile):
            port = RecordingPort(profile)
            port.profile_key = "profile-for-someone-else"
            return port

        registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)),
            browser_factory=factory)
        with self.assertRaises(ChatGPTWebUnavailable):
            registry.register(identity())
        with self.assertRaises(WebAgentScopeError):
            registry.profile(owner_id="owner-a", project_id="project-one",
                             agent_id="agent-one")

    def test_disabled_and_missing_factory_do_not_register_profiles(self):
        calls = []

        def factory(profile):
            calls.append(profile)
            return RecordingPort(profile)

        disabled = WebAgentSessionRegistry(
            ChatGPTWebConfig(models=("fake-model",)), browser_factory=factory)
        with self.assertRaises(ChatGPTWebUnavailable):
            disabled.register(identity())
        enabled_no_factory = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)))
        with self.assertRaises(ChatGPTWebUnavailable):
            enabled_no_factory.register(identity())
        self.assertEqual(calls, [])

    def test_factory_error_redacted_and_no_registration(self):
        registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)),
            browser_factory=lambda _: (_ for _ in ()).throw(
                RuntimeError("SECRET-in-factory")))
        with self.assertRaises(ChatGPTWebUnavailable) as error:
            registry.register(identity())
        self.assertNotIn("SECRET", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        with self.assertRaises(WebAgentScopeError):
            registry.profile(owner_id="owner-a", project_id="project-one",
                             agent_id="agent-one")


class AgentQueueTests(unittest.TestCase):
    def test_blocked_agent_does_not_block_another_agents_queue(self):
        entered = Event()
        release = Event()
        ports = {}

        def factory(profile):
            port = RecordingPort(profile, block_first=release if profile.agent_id == "a" else None,
                                 entered=entered if profile.agent_id == "a" else None)
            ports[profile.agent_id] = port
            return port

        registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)),
            browser_factory=factory)
        registry.register(identity(agent="a"))
        registry.register(identity(agent="b"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_agent, registry, agent="a", prompt="slow")
            self.assertTrue(entered.wait(timeout=8))
            second = pool.submit(run_agent, registry, agent="b", prompt="fast")
            try:
                self.assertEqual(second.result(timeout=8).answer, "b:fast")
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=8).answer, "a:slow")
        self.assertIsNot(ports["a"], ports["b"])

    def test_same_agent_fifo_and_queue_advances_after_unknown_driver_error(self):
        entered, release = Event(), Event()
        port_box = []

        def factory(profile):
            port = RecordingPort(profile, block_first=release, entered=entered,
                                 error_first=True)
            port_box.append(port)
            return port

        registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("fake-model",)),
            browser_factory=factory)
        registry.register(identity())
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_agent, registry, prompt="first")
            self.assertTrue(entered.wait(timeout=8))
            second = pool.submit(run_agent, registry, prompt="second")
            cutoff = time.monotonic() + 8
            while registry.pending(owner_id="owner-a", project_id="project-one",
                                   agent_id="agent-one") != 2 and time.monotonic() < cutoff:
                time.sleep(0.005)
            self.assertEqual(registry.pending(owner_id="owner-a", project_id="project-one",
                                              agent_id="agent-one"), 2)
            self.assertEqual(len(port_box[0].calls), 1)
            release.set()
            self.assertEqual(first.result(timeout=8).status, "failed")
            self.assertEqual(second.result(timeout=8).answer, "agent-one:second")
        self.assertEqual([messages[-1].content for _, messages in port_box[0].calls],
                         ["first", "second"])
        self.assertEqual(registry.pending(owner_id="owner-a", project_id="project-one",
                                          agent_id="agent-one"), 0)


if __name__ == "__main__":
    unittest.main()
