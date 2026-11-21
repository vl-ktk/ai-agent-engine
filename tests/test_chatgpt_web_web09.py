"""WEB-09: measured/bounded/unknown context pressure before mock turns."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, ContextThresholds,
    ContextUsageEvidence, CreatedProjectChat, ModelSelectionEvidence,
    ProjectChatManager, ProjectMembershipEvidence, TrustedAgentContextPolicy,
    TrustedAgentModelPolicy, TrustedChatGPTProjectBinding,
    TrustedChatGPTProjectRegistry, TrustedProjectModelCatalog,
    WebAgentIdentity, WebAgentModelRegistry, WebAgentSessionRegistry,
    WebContextMonitor, WebContextPressureError,
)


class Membership:
    def __init__(self, calls):
        self.calls = calls

    def verify(self, *, binding, profile):
        self.calls.append(("membership", profile.agent_id))
        return ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, True)


class ModelSelector:
    def __init__(self, calls):
        self.calls = calls

    def select(self, *, binding, profile, model_id):
        self.calls.append(("model", profile.agent_id))
        return ModelSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, True)


class ChatPort:
    def __init__(self, profile, calls):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.agent_id = profile.agent_id
        self.calls = calls
        self.sequence = 0

    def create_chat(self, *, chatgpt_project_id, predecessor_id):
        self.calls.append(("chat", chatgpt_project_id))
        self.sequence += 1
        return CreatedProjectChat(
            f"{self.agent_id}-{chatgpt_project_id}-chat-{self.sequence}",
            chatgpt_project_id, self.profile_key, self.mailbox_key)


class Browser:
    def __init__(self, profile, calls):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.agent_id = profile.agent_id
        self.calls = calls
        self.prompts = []

    def complete(self, *, model_id, messages):
        self.calls.append(("browser", self.agent_id))
        self.prompts.append(messages)
        return BrowserTurn(model_id, "ACK")


class Usage:
    def __init__(self, calls):
        self.calls = calls
        self.used = 0
        self.upper = 0
        self.kind = "exact"
        self.current = True
        self.verified = True
        self.mutator = None
        self.entered = None
        self.release = None

    def read_current(self, *, binding, profile, conversation_id):
        self.calls.append(("usage", profile.agent_id))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(8):
            raise RuntimeError("usage source timed out")
        result = ContextUsageEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, conversation_id, self.kind,
            self.used, self.upper, self.current, self.verified)
        return self.mutator(result) if self.mutator else result


class PolicyTests(unittest.TestCase):
    def test_ordered_configurable_thresholds_reject_invalid_values(self):
        self.assertEqual(ContextThresholds(), ContextThresholds(60, 75, 90))
        self.assertEqual(ContextThresholds(55, 70, 88).rotate_percent, 70)
        for value in ((0, 75, 90), (75, 75, 90), (80, 75, 90),
                      (60, 91, 90), (60, 75, 101), (True, 75, 90),
                      (60.0, 75, 90), (60, "75", 90)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ContextThresholds(*value)

    def test_reject_malformed_budget_and_forged_policy(self):
        values = dict(owner_id="owner", project_id="one", agent_id="a",
                      chatgpt_project_id="gpt-one", context_window_tokens=10_000)
        for change in (dict(owner_id="../bad"), dict(agent_id=""),
                       dict(context_window_tokens=True),
                       dict(context_window_tokens=0),
                       dict(context_window_tokens=100_000_001),
                       dict(thresholds="60/75/90"),
                       dict(response_reserve_tokens=-1),
                       dict(response_reserve_tokens=10_000),
                       dict(response_reserve_tokens=True)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                TrustedAgentContextPolicy(**dict(values, **change))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.browsers = {}
        self.usage = Usage(self.calls)

        def browser_factory(profile):
            browser = Browser(profile, self.calls)
            self.browsers[profile.agent_id] = browser
            return browser

        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant",)),
            browser_factory=browser_factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("owner", "one", "gpt-one"),
                TrustedChatGPTProjectBinding("owner", "two", "gpt-two"))),
            project_verifier=Membership(self.calls))
        self.chats = ProjectChatManager(
            self.sessions,
            chat_port_factory=lambda profile: ChatPort(profile, self.calls))
        self.monitor = WebContextMonitor(
            self.sessions, self.chats, usage_source=self.usage)
        self.models = WebAgentModelRegistry(
            self.sessions, project_catalogs=(
                TrustedProjectModelCatalog("gpt-one", ("instant",)),
                TrustedProjectModelCatalog("gpt-two", ("instant",))),
            selector=ModelSelector(self.calls),
            context_monitor=self.monitor)
        self.register()

    def scope(self, agent="a", project="one", owner="owner"):
        return dict(owner_id=owner, project_id=project, agent_id=agent)

    def policy(self, agent="a", project="one", gpt_project="gpt-one",
               window=10_000, thresholds=None, reserve=0):
        return TrustedAgentContextPolicy(
            "owner", project, agent, gpt_project, window,
            thresholds if thresholds is not None else ContextThresholds(), reserve)

    def register(self, agent="a", project="one", gpt_project="gpt-one"):
        scope = self.scope(agent, project)
        self.sessions.register(WebAgentIdentity(
            "owner", project, agent, AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner", project, agent, "instant", ("instant",)))
        self.models.register_context(self.policy(agent, project, gpt_project))
        self.chats.create_initial(**scope)

    def turn(self, **override):
        return self.models.run(**dict(self.scope(), prompt="go", **override))

    def projected_overhead(self):
        # Byte upper bound includes the complete generated system message.
        return len("go".encode()) + len(
            "Agent role: executor\nProject: one\nAgent: a".encode()) + 32

    def test_all_exact_threshold_boundaries_and_no_premature_rotation(self):
        overhead = self.projected_overhead()
        examples = ((5999, "OK", True), (6000, "WARN", True),
                    (7499, "WARN", True), (7500, "ROTATE", False),
                    (8999, "ROTATE", False), (9000, "EMERGENCY", False),
                    (10_000, "EMERGENCY", False))
        for target, expected, send in examples:
            self.usage.used = target - overhead
            self.usage.upper = target - overhead
            before = len(self.browsers["a"].prompts)
            with self.subTest(target=target):
                if send:
                    self.assertEqual(self.turn().status, "done")
                else:
                    with self.assertRaises(WebContextPressureError):
                        self.turn()
                decision = self.monitor.last_decision(**self.scope())
                self.assertEqual(decision.action, expected)
                self.assertEqual(decision.projected_upper_tokens, target)
                self.assertEqual(len(self.browsers["a"].prompts),
                                 before + int(send))
        self.assertEqual(len(self.chats.history(**self.scope())), 1)

    def test_custom_thresholds_and_response_reserve(self):
        # Separate agent makes the policy immutable after registration.
        self.sessions.register(WebAgentIdentity(
            "owner", "one", "custom", AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner", "one", "custom", "instant", ("instant",)))
        self.models.register_context(self.policy(
            agent="custom", thresholds=ContextThresholds(50, 65, 80),
            reserve=300))
        self.chats.create_initial(**self.scope(agent="custom"))
        self.usage.used = self.usage.upper = 6250
        with self.assertRaises(WebContextPressureError):
            self.models.run(**self.scope(agent="custom"), prompt="go")
        self.assertEqual(self.monitor.last_decision(
            **self.scope(agent="custom")).action, "ROTATE")

    def test_bounded_estimate_uses_upper_not_optimistic_point(self):
        self.usage.kind = "bounded"
        self.usage.used = 1000
        self.usage.upper = 7600
        with self.assertRaises(WebContextPressureError):
            self.turn()
        decision = self.monitor.last_decision(**self.scope())
        self.assertEqual((decision.action, decision.estimate_kind),
                         ("ROTATE", "bounded"))
        self.assertFalse(self.browsers["a"].prompts)

    def test_unevidenced_unknown_usage_is_hold(self):
        self.usage.kind = "unknown"
        self.usage.used = self.usage.upper = None
        with self.assertRaisesRegex(WebContextPressureError, "hold"):
            self.turn()
        decision = self.monitor.last_decision(**self.scope())
        self.assertEqual(decision.action, "HOLD")
        self.assertIsNone(decision.projected_upper_tokens)
        self.assertFalse(self.browsers["a"].prompts)

    def test_unbounded_point_estimate_and_malformed_unknown_refused(self):
        self.usage.kind = "bounded"
        self.usage.used = 5
        self.usage.upper = None
        with self.assertRaises(WebContextPressureError):
            self.turn()
        self.usage.kind = "unknown"
        with self.assertRaises(WebContextPressureError):
            self.turn()
        self.assertIsNone(self.monitor.last_decision(**self.scope()))

    def test_false_or_malformed_counts_fail_closed(self):
        for kind, used, upper in (("exact", -1, -1),
                                  ("bounded", 90, 89),
                                  ("exact", 3, 4),
                                  ("bounded", True, 12),
                                  ("exact", 3.0, 3.0),
                                  ("exact", 1, True),
                                  ("unknownish", None, None),
                                  ("bounded", 1, 100_000_001)):
            self.usage.kind, self.usage.used, self.usage.upper = kind, used, upper
            with self.subTest(kind=kind, used=used, upper=upper):
                with self.assertRaises(WebContextPressureError):
                    self.turn()
                self.assertIsNone(self.monitor.last_decision(**self.scope()))
        self.assertFalse(self.browsers["a"].prompts)

    def test_proof_mismatch_stale_unverified_or_wrong_type_fails_closed(self):
        self.usage.used = self.usage.upper = 0
        for name, value in (("owner_id", "other"), ("project_id", "two"),
                            ("agent_id", "b"),
                            ("chatgpt_project_id", "gpt-two"),
                            ("profile_key", "foreign"),
                            ("mailbox_key", "foreign"),
                            ("conversation_id", "wrong"),
                            ("current", False), ("current", 1),
                            ("verified", False), ("verified", 1)):
            self.usage.mutator = lambda p, n=name, v=value: replace(p, **{n: v})
            with self.subTest(name=name), self.assertRaises(WebContextPressureError):
                self.turn()
        for mutate in (lambda p: None, lambda p: {},
                       lambda p: (_ for _ in ()).throw(RuntimeError("secret"))):
            self.usage.mutator = mutate
            with self.assertRaises(WebContextPressureError) as error:
                self.turn()
            self.assertNotIn("secret", str(error.exception))
        self.assertFalse(self.browsers["a"].prompts)

    def test_counter_regression_blocks_but_new_chat_resets_baseline(self):
        self.usage.used = self.usage.upper = 5000
        self.turn()
        self.usage.used = self.usage.upper = 4999
        with self.assertRaisesRegex(WebContextPressureError, "regressed"):
            self.turn()
        self.assertIsNone(self.monitor.last_decision(**self.scope()))
        self.chats.rotate(**self.scope())
        self.usage.used = self.usage.upper = 0
        self.turn()
        self.assertEqual(len(self.chats.history(**self.scope())), 2)
        self.assertEqual(len(self.browsers["a"].prompts), 2)

    def test_per_agent_scope_no_cross_project_usage(self):
        self.register("b", "two", "gpt-two")
        self.usage.used = self.usage.upper = 0
        self.models.run(**self.scope("b", "two"), prompt="safe")
        self.assertEqual(self.browsers["b"].prompts[-1][-1].content, "safe")
        for denied in (self.scope("a", "two"),
                       self.scope("b", "one"),
                       self.scope("a", "one", "other")):
            with self.subTest(denied=denied), self.assertRaises(Exception):
                self.models.run(**denied, prompt="foreign")
        self.assertEqual(len(self.browsers["a"].prompts), 0)
        self.assertEqual(len(self.browsers["b"].prompts), 1)

    def test_no_active_chat_or_missing_policy_stops_before_usage(self):
        self.sessions.register(WebAgentIdentity(
            "owner", "one", "c", AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner", "one", "c", "instant", ("instant",)))
        with self.assertRaises(WebContextPressureError):
            self.models.run(**self.scope(agent="c"), prompt="blocked")
        self.models.register_context(self.policy(agent="c"))
        with self.assertRaises(WebContextPressureError):
            self.models.run(**self.scope(agent="c"), prompt="blocked")
        self.assertNotIn(("usage", "c"), self.calls)

    def test_duplicate_policy_foreign_policy_and_monitor_dependencies(self):
        with self.assertRaises(TypeError):
            self.models.register_context(object())
        with self.assertRaises(WebContextPressureError):
            self.models.register_context(self.policy())
        with self.assertRaises(WebContextPressureError):
            self.models.register_context(self.policy(gpt_project="gpt-two"))
        with self.assertRaises(ValueError):
            WebContextMonitor(self.sessions, self.chats, usage_source=object())

    def test_observation_inside_agent_fifo_precedes_browser(self):
        self.usage.used = self.usage.upper = 0
        self.turn()
        self.assertEqual(self.calls[-3:], [
            ("model", "a"), ("usage", "a"), ("browser", "a")])

    def test_same_agent_serializes_usage_and_chat_rotation(self):
        self.usage.used = self.usage.upper = 0
        self.usage.entered = Event()
        self.usage.release = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.turn)
            self.assertTrue(self.usage.entered.wait(5))
            waiting = pool.submit(self.chats.rotate, **self.scope())
            self.assertEqual(self.sessions.pending(**self.scope()), 2)
            self.assertFalse(waiting.done())
            self.usage.release.set()
            self.assertEqual(running.result(timeout=5).status, "done")
            self.assertEqual(waiting.result(timeout=5).predecessor_id,
                             "a-gpt-one-chat-1")


if __name__ == "__main__":
    unittest.main()
