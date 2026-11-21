"""WEB-08: scoped versioned instructions load before every mock browser turn."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from threading import Event
import time
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, InstructionGrantEvidence,
    ModelSelectionEvidence, ProjectMembershipEvidence, ReasoningSelectionEvidence,
    ScopedInstructionSnapshot, TrustedAgentInstructionPolicy,
    TrustedAgentModelPolicy, TrustedAgentReasoningPolicy,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
    TrustedModelReasoningChoices, TrustedProjectModelCatalog,
    TrustedProjectReasoningCatalog, WebAgentIdentity, WebAgentModelRegistry,
    WebAgentScopeError, WebAgentSessionRegistry, WebInstructionBootstrap,
    WebInstructionBootstrapError, WebModelSelectionError,
    WebReasoningSelectionError,
)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Member:
    def __init__(self, calls):
        self.calls = calls
        self.enabled = True

    def verify(self, *, binding, profile):
        self.calls.append(("member", profile.agent_id))
        return ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, self.enabled)


class ModelSelector:
    def __init__(self, calls):
        self.calls = calls
        self.valid = True

    def select(self, *, binding, profile, model_id):
        self.calls.append(("model", profile.agent_id))
        return ModelSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, self.valid)


class ReasoningSelector:
    def __init__(self, calls):
        self.calls = calls
        self.valid = True

    def select(self, *, binding, profile, model_id, reasoning_effort):
        self.calls.append(("reasoning", profile.agent_id))
        return ReasoningSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, reasoning_effort, self.valid)


class Memory:
    def __init__(self, calls):
        self.calls = calls
        self.content = "Trusted project instructions v3"
        self.revision = 3
        self.mutator = None
        self.entered = None
        self.release = None

    def read_current(self, *, binding, profile, role):
        self.calls.append(("memory", profile.agent_id))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(12):
            raise RuntimeError("fake memory timeout")
        result = ScopedInstructionSnapshot(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, role, self.revision, self.content, True, True)
        return self.mutator(result) if self.mutator else result


class Grants:
    def __init__(self, calls):
        self.calls = calls
        self.mutator = None

    def verify(self, *, binding, profile, role, revision, required_actions):
        self.calls.append(("grant", profile.agent_id))
        result = InstructionGrantEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, role, revision, required_actions, True)
        return self.mutator(result) if self.mutator else result


class Browser:
    def __init__(self, profile, calls):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = calls
        self.history = []
        self.agent_id = profile.agent_id

    def complete(self, *, model_id, messages):
        self.calls.append(("browser", self.agent_id))
        self.history.append(messages)
        return BrowserTurn(model_id, "ACK:" + messages[-1].content)


class PolicyValidationTests(unittest.TestCase):
    def test_reject_bad_revision_digest_scope_and_required_actions(self):
        values = dict(owner_id="owner", project_id="project", agent_id="agent",
                      chatgpt_project_id="gpt-project", role=AgentRole.EXECUTOR,
                      revision=3, instructions_sha256=digest("valid"),
                      required_actions=("READ_MEMORY",))
        for changes in (dict(revision=0), dict(revision=True),
                        dict(instructions_sha256="fake"),
                        dict(instructions_sha256=digest("valid").upper()),
                        dict(role="executor"), dict(agent_id="../foreign"),
                        dict(required_actions=["READ_MEMORY"]),
                        dict(required_actions=("READ_MEMORY", "READ_MEMORY")),
                        dict(required_actions=("grant now",))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                TrustedAgentInstructionPolicy(**dict(values, **changes))

    def test_requires_real_injected_ports(self):
        for source, grants in ((None, object()), (object(), None),
                               (object(), object())):
            with self.subTest(source=source), self.assertRaises(ValueError):
                WebInstructionBootstrap(source=source, grant_verifier=grants)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.ports = {}
        self.member = Member(self.calls)
        self.model_selector = ModelSelector(self.calls)
        self.reasoning_selector = ReasoningSelector(self.calls)
        self.memory = Memory(self.calls)
        self.grants = Grants(self.calls)
        self.bootstrap = WebInstructionBootstrap(source=self.memory,
                                                  grant_verifier=self.grants)

        def factory(profile):
            port = Browser(profile, self.calls)
            self.ports[profile.agent_id] = port
            return port

        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("thinking",)),
            browser_factory=factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("owner", "project-one", "gpt-one"),
                TrustedChatGPTProjectBinding("owner", "project-two", "gpt-two"))),
            project_verifier=self.member)
        self.models = WebAgentModelRegistry(
            self.sessions,
            project_catalogs=(TrustedProjectModelCatalog("gpt-one", ("thinking",)),
                              TrustedProjectModelCatalog("gpt-two", ("thinking",))),
            selector=self.model_selector,
            reasoning_catalogs=(
                TrustedProjectReasoningCatalog("gpt-one", (
                    TrustedModelReasoningChoices("thinking", ("Medium",)),)),
                TrustedProjectReasoningCatalog("gpt-two", (
                    TrustedModelReasoningChoices("thinking", ("Medium",)),))),
            reasoning_selector=self.reasoning_selector,
            instruction_bootstrap=self.bootstrap)

    def scope(self, agent="agent-one", project="project-one", owner="owner"):
        return dict(owner_id=owner, project_id=project, agent_id=agent)

    def policy(self, agent="agent-one", project="project-one",
               gpt_project="gpt-one", role=AgentRole.EXECUTOR,
               actions=("READ_MEMORY",), revision=3, content=None):
        return TrustedAgentInstructionPolicy(
            "owner", project, agent, gpt_project, role, revision,
            digest(self.memory.content if content is None else content), actions)

    def register(self, agent="agent-one", project="project-one",
                 gpt_project="gpt-one", role=AgentRole.EXECUTOR,
                 instructions="", declared_actions=("READ_MEMORY",),
                 with_instructions=True, actions=("READ_MEMORY",)):
        self.sessions.register(WebAgentIdentity(
            "owner", project, agent, role, instructions, declared_actions))
        self.models.register(TrustedAgentModelPolicy(
            "owner", project, agent, "thinking", ("thinking",)))
        self.models.register_reasoning(TrustedAgentReasoningPolicy(
            "owner", project, agent, "Medium", ("Medium",)))
        if with_instructions:
            self.models.register_instructions(self.policy(
                agent, project, gpt_project, role, actions))

    def test_verified_bootstrap_reaches_system_message_before_browser(self):
        self.register()
        result = self.models.run(**self.scope(), prompt="do work")
        self.assertEqual(result.answer, "ACK:do work")
        messages = self.ports["agent-one"].history[0]
        self.assertIn("Instructions: " + self.memory.content, messages[0].content)
        self.assertIn("Agent role: executor", messages[0].content)
        self.assertEqual(self.calls,
                         [("member", "agent-one"), ("model", "agent-one"),
                          ("reasoning", "agent-one"), ("memory", "agent-one"),
                          ("grant", "agent-one"), ("browser", "agent-one")])

    def test_missing_policy_fails_without_memory_or_send(self):
        self.register(with_instructions=False)
        with self.assertRaises(WebInstructionBootstrapError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.calls[-1], ("reasoning", "agent-one"))
        self.assertEqual(self.ports["agent-one"].history, [])

    def test_stale_and_future_revision_refused(self):
        self.register()
        for revision in (2, 4):
            self.memory.revision = revision
            with self.subTest(revision=revision), self.assertRaises(
                    WebInstructionBootstrapError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertNotIn(("grant", "agent-one"), self.calls)

    def test_changed_instructions_require_new_trusted_digest(self):
        self.register()
        self.memory.content = "Changed behind the owner's back"
        with self.assertRaises(WebInstructionBootstrapError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].history, [])

    def test_every_snapshot_scope_field_must_match(self):
        self.register()
        for name, value in (("owner_id", "other"), ("project_id", "project-two"),
                            ("agent_id", "agent-two"),
                            ("chatgpt_project_id", "gpt-two"),
                            ("profile_key", "wrong"), ("mailbox_key", "wrong"),
                            ("role", AgentRole.SUPERVISOR)):
            self.memory.mutator = lambda p, n=name, v=value: replace(p, **{n: v})
            with self.subTest(field=name), self.assertRaises(
                    WebInstructionBootstrapError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].history, [])

    def test_unverified_nonbool_stale_and_malformed_memory_proofs_refused(self):
        self.register()
        changes = (lambda p: replace(p, verified=False),
                   lambda p: replace(p, verified=1),
                   lambda p: replace(p, current=False),
                   lambda p: replace(p, current=1),
                   lambda p: None, lambda p: {},
                   lambda p: replace(p, revision=True))
        for mutate in changes:
            self.memory.mutator = mutate
            with self.subTest(mutate=mutate), self.assertRaises(
                    WebInstructionBootstrapError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].history, [])

    def test_empty_nul_and_oversized_memory_content_refused(self):
        self.register()
        for content in ("", "   ", "bad\x00secret", "X" * 8_001):
            self.memory.mutator = lambda p, t=content: replace(p, instructions=t)
            with self.subTest(size=len(content)), self.assertRaises(
                    WebInstructionBootstrapError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].history, [])

    def test_grant_must_prove_every_exact_scope_field_role_revision_and_actions(self):
        self.register()
        for name, value in (("owner_id", "foreign"), ("project_id", "project-two"),
                            ("agent_id", "agent-two"),
                            ("chatgpt_project_id", "gpt-two"),
                            ("profile_key", "wrong"), ("mailbox_key", "wrong"),
                            ("role", AgentRole.SUPERVISOR), ("revision", 4),
                            ("required_actions", ()),
                            ("required_actions", ("WRITE_WORKSPACE",))):
            self.grants.mutator = lambda p, n=name, v=value: replace(p, **{n: v})
            with self.subTest(name=name), self.assertRaises(
                    WebInstructionBootstrapError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].history, [])

    def test_unverified_nonbool_malformed_or_exception_grant_is_fail_closed(self):
        self.register()
        changes = (lambda p: replace(p, verified=False),
                   lambda p: replace(p, verified=1),
                   lambda p: replace(p, revision=True),
                   lambda p: replace(p, required_actions=["READ_MEMORY"]),
                   lambda p: None, lambda p: {},
                   lambda p: (_ for _ in ()).throw(RuntimeError("private secret")))
        for mutate in changes:
            self.grants.mutator = mutate
            with self.subTest(mutate=mutate), self.assertRaises(
                    WebInstructionBootstrapError) as error:
                self.models.run(**self.scope(), prompt="blocked")
            self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.ports["agent-one"].history, [])

    def test_memory_exception_is_sanitized_and_grant_not_called(self):
        self.register()
        self.memory.mutator = lambda p: (_ for _ in ()).throw(
            RuntimeError("private memory secret"))
        with self.assertRaises(WebInstructionBootstrapError) as error:
            self.models.run(**self.scope(), prompt="blocked")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.calls[-1], ("memory", "agent-one"))

    def test_identity_role_project_grant_and_legacy_prompt_guard_registration(self):
        self.register(with_instructions=False)
        for bad in (self.policy(gpt_project="gpt-two"),
                    self.policy(role=AgentRole.SUPERVISOR),
                    self.policy(actions=("WRITE_WORKSPACE",)),
                    self.policy(project="project-two")):
            with self.subTest(bad=bad), self.assertRaises(WebAgentScopeError):
                self.models.register_instructions(bad)
        # None of the failures can poison the correct later registration.
        self.models.register_instructions(self.policy())
        self.assertEqual(self.models.run(**self.scope(), prompt="ok").answer,
                         "ACK:ok")

    def test_legacy_inline_instructions_cannot_mix_with_scoped_revision(self):
        self.register(instructions="legacy role text", with_instructions=False)
        with self.assertRaises(WebInstructionBootstrapError):
            self.models.register_instructions(self.policy())

    def test_policy_rebinding_refused_even_with_new_revision(self):
        self.register()
        with self.assertRaises(WebInstructionBootstrapError):
            self.models.register_instructions(self.policy(revision=4))
        self.models.run(**self.scope(), prompt="original")

    def test_cross_owner_project_and_agent_are_denied(self):
        self.register()
        for wrong in (self.scope(owner="foreign"),
                      self.scope(project="project-two"),
                      self.scope(agent="missing")):
            with self.subTest(wrong=wrong), self.assertRaises(WebAgentScopeError):
                self.models.run(**wrong, prompt="blocked")
        self.assertEqual(self.calls, [])

    def test_other_project_agent_gets_only_own_scoped_instructions(self):
        self.register()
        self.register("agent-two", "project-two", "gpt-two")
        self.models.run(**self.scope("agent-two", "project-two"), prompt="second")
        self.models.run(**self.scope(), prompt="first")
        self.assertNotIn("agent-one", self.ports["agent-two"].history[0][0].content)
        self.assertIn("Project: project-two",
                      self.ports["agent-two"].history[0][0].content)
        self.assertEqual(len(self.ports["agent-one"].history), 1)

    def test_membership_model_and_reasoning_fail_before_memory(self):
        self.register()
        for gate, error_type in ((self.member, WebAgentScopeError),
                                 (self.model_selector, WebModelSelectionError),
                                 (self.reasoning_selector, WebReasoningSelectionError)):
            if gate is self.member:
                gate.enabled = False
            else:
                gate.valid = False
            with self.subTest(gate=gate), self.assertRaises(error_type):
                self.models.run(**self.scope(), prompt="blocked")
            gate.enabled = True if gate is self.member else getattr(gate, "enabled", True)
            if gate is not self.member:
                gate.valid = True
        self.assertFalse(any(name in ("memory", "browser")
                             for name, _ in self.calls))

    def test_fresh_memory_and_grant_proofs_on_every_turn(self):
        self.register()
        self.models.run(**self.scope(), prompt="first")
        self.grants.mutator = lambda p: replace(p, verified=False)
        with self.assertRaises(WebInstructionBootstrapError):
            self.models.run(**self.scope(), prompt="second")
        self.assertEqual(self.calls.count(("memory", "agent-one")), 2)
        self.assertEqual(self.calls.count(("grant", "agent-one")), 2)
        self.assertEqual(len(self.ports["agent-one"].history), 1)

    def test_memory_proof_and_browser_delivery_share_fifo(self):
        self.register()
        entered, release = Event(), Event()
        self.memory.entered, self.memory.release = entered, release
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.models.run, **self.scope(), prompt="one")
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(self.models.run, **self.scope(), prompt="two")
            time.sleep(0.04)
            self.assertEqual(self.calls.count(("memory", "agent-one")), 1)
            self.assertEqual(self.ports["agent-one"].history, [])
            release.set()
            self.assertEqual(first.result(timeout=6).answer, "ACK:one")
            self.assertEqual(second.result(timeout=6).answer, "ACK:two")

    def test_failed_bootstrap_releases_fifo_for_next_safe_turn(self):
        self.register()
        self.grants.mutator = lambda p: replace(p, verified=False)
        with self.assertRaises(WebInstructionBootstrapError):
            self.models.run(**self.scope(), prompt="blocked")
        self.grants.mutator = None
        self.assertEqual(self.models.run(**self.scope(), prompt="next").answer,
                         "ACK:next")

    def test_disabled_bootstrap_rejects_registration(self):
        old = WebAgentModelRegistry(
            self.sessions,
            project_catalogs=(TrustedProjectModelCatalog("gpt-one", ("thinking",)),),
            selector=self.model_selector)
        self.register(with_instructions=False)
        with self.assertRaises(WebAgentScopeError):
            old.register_instructions(self.policy())

    def test_constructor_rejects_untrusted_bootstrap(self):
        with self.assertRaises(ValueError):
            WebAgentModelRegistry(
                self.sessions,
                project_catalogs=(TrustedProjectModelCatalog("gpt-one", ("thinking",)),),
                selector=self.model_selector, instruction_bootstrap=object())


if __name__ == "__main__":
    unittest.main()
