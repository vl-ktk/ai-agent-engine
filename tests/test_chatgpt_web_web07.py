"""WEB-07: model-scoped mock reasoning choices must be proven before send."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import time
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, ModelSelectionEvidence,
    ProjectMembershipEvidence, ReasoningSelectionEvidence,
    TrustedAgentModelPolicy, TrustedAgentReasoningPolicy,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
    TrustedModelReasoningChoices, TrustedProjectModelCatalog,
    TrustedProjectReasoningCatalog, WebAgentIdentity, WebAgentModelRegistry,
    WebAgentScopeError, WebAgentSessionRegistry, WebModelSelectionError,
    WebReasoningSelectionError,
)


class Member:
    def __init__(self):
        self.verified = True
        self.calls = []

    def verify(self, *, binding, profile):
        self.calls.append(profile.agent_id)
        return ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, self.verified)


class Browser:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = []

    def complete(self, *, model_id, messages):
        self.calls.append((model_id, messages[-1].content))
        return BrowserTurn(model_id, "OK:" + model_id)


class ModelSelector:
    def __init__(self):
        self.calls = []
        self.mutator = None

    def select(self, *, binding, profile, model_id):
        self.calls.append((profile.agent_id, model_id))
        proof = ModelSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, True)
        return self.mutator(proof) if self.mutator else proof


class EffortSelector:
    def __init__(self):
        self.calls = []
        self.mutator = None
        self.entered = None
        self.release = None

    def select(self, *, binding, profile, model_id, reasoning_effort):
        self.calls.append((profile.agent_id, model_id, reasoning_effort))
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(12):
            raise RuntimeError("mock selector timeout")
        proof = ReasoningSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, reasoning_effort, True)
        return self.mutator(proof) if self.mutator else proof


class ReasoningSchemaTests(unittest.TestCase):
    def test_choices_reject_duplicate_malformed_and_empty_values(self):
        for efforts in ((), [], ("Medium", "Medium"), ("bad effort",),
                        ("../bad",), (None,), (True,), ("",)):
            with self.subTest(efforts=efforts), self.assertRaises(ValueError):
                TrustedModelReasoningChoices("thinking", efforts)
        with self.assertRaises(ValueError):
            TrustedModelReasoningChoices("bad model", ("Medium",))

    def test_project_catalog_rejects_duplicate_models_and_invalid_scope(self):
        model = TrustedModelReasoningChoices("thinking", ("Medium",))
        for choices in ((), [], (model, model), (object(),)):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                TrustedProjectReasoningCatalog("gpt-one", choices)
        with self.assertRaises(ValueError):
            TrustedProjectReasoningCatalog("../gpt", (model,))

    def test_agent_policy_rejects_invalid_defaults_and_scope(self):
        data = dict(owner_id="owner", project_id="project", agent_id="agent",
                    default_reasoning_effort="Instant",
                    allowed_reasoning_efforts=("Instant", "Medium"))
        for change in (dict(allowed_reasoning_efforts=()),
                       dict(allowed_reasoning_efforts=["Instant"]),
                       dict(default_reasoning_effort="High"),
                       dict(allowed_reasoning_efforts=("Instant", "Instant")),
                       dict(allowed_reasoning_efforts=("Instant", "oops!")),
                       dict(agent_id="../foreign")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                TrustedAgentReasoningPolicy(**dict(data, **change))


class ReasoningRegistryTests(unittest.TestCase):
    def setUp(self):
        self.ports = {}
        self.member = Member()
        self.model_selector = ModelSelector()
        self.effort_selector = EffortSelector()

        def factory(profile):
            port = Browser(profile)
            self.ports[profile.agent_id] = port
            return port

        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant", "thinking", "other")),
            browser_factory=factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("owner", "project-one", "gpt-one"),
                TrustedChatGPTProjectBinding("owner", "project-two", "gpt-two"))),
            project_verifier=self.member)
        self.model_catalogs = (
            TrustedProjectModelCatalog("gpt-one", ("instant", "thinking")),
            TrustedProjectModelCatalog("gpt-two", ("other",)))
        self.reasoning_catalogs = (
            TrustedProjectReasoningCatalog("gpt-one", (
                TrustedModelReasoningChoices("instant", ("Instant",)),
                TrustedModelReasoningChoices("thinking", ("Medium", "High")))),
            TrustedProjectReasoningCatalog("gpt-two", (
                TrustedModelReasoningChoices("other", ("Instant",)),)))
        self.models = self.new_registry()

    def new_registry(self, **overrides):
        args = dict(project_catalogs=self.model_catalogs,
                    selector=self.model_selector,
                    reasoning_catalogs=self.reasoning_catalogs,
                    reasoning_selector=self.effort_selector)
        args.update(overrides)
        return WebAgentModelRegistry(self.sessions, **args)

    def scope(self, agent="agent-one", project="project-one", owner="owner"):
        return dict(owner_id=owner, project_id=project, agent_id=agent)

    def register(self, agent="agent-one", project="project-one",
                 model="instant", allowed_models=("instant", "thinking"),
                 effort="Instant", allowed_efforts=("Instant", "Medium", "High")):
        self.sessions.register(WebAgentIdentity("owner", project, agent,
                                                AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner", project, agent, model, allowed_models))
        self.models.register_reasoning(TrustedAgentReasoningPolicy(
            "owner", project, agent, effort, allowed_efforts))

    def test_default_model_effort_and_explicit_overrides(self):
        self.register()
        first = self.models.run(**self.scope(), prompt="first")
        second = self.models.run(**self.scope(), prompt="second",
                                 requested_model_id="thinking",
                                 requested_reasoning_effort="High")
        self.assertEqual((first.answer, second.answer),
                         ("OK:instant", "OK:thinking"))
        self.assertEqual(self.effort_selector.calls,
                         [("agent-one", "instant", "Instant"),
                          ("agent-one", "thinking", "High")])
        self.assertEqual(len(self.ports["agent-one"].calls), 2)

    def test_unavailable_effort_for_selected_model_never_falls_back(self):
        self.register()
        for args in (dict(requested_reasoning_effort="High"),
                     dict(requested_model_id="thinking"),
                     dict(requested_reasoning_effort="Unknown"),
                     dict(requested_reasoning_effort=""),
                     dict(requested_reasoning_effort=123),
                     dict(requested_reasoning_effort=True)):
            with self.subTest(args=args), self.assertRaises(WebReasoningSelectionError):
                self.models.run(**self.scope(), prompt="blocked", **args)
        self.assertEqual(self.model_selector.calls, [])
        self.assertEqual(self.effort_selector.calls, [])
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_agent_cannot_use_effort_not_in_own_policy(self):
        self.register(allowed_efforts=("Instant",))
        with self.assertRaises(WebReasoningSelectionError):
            self.models.run(**self.scope(), prompt="blocked",
                            requested_model_id="thinking",
                            requested_reasoning_effort="Medium")
        self.assertEqual(self.model_selector.calls, [])

    def test_cross_project_model_effort_catalogs_remain_separate(self):
        self.register()
        self.register("agent-two", "project-two", "other", ("other",),
                      "Instant", ("Instant",))
        self.assertEqual(self.models.run(**self.scope("agent-two", "project-two"),
                                         prompt="own").answer, "OK:other")
        with self.assertRaises(WebReasoningSelectionError):
            self.models.run(**self.scope("agent-two", "project-two"),
                            prompt="foreign", requested_reasoning_effort="High")
        self.assertEqual(len(self.ports["agent-two"].calls), 1)

    def test_unregistered_effort_policy_blocks_delivery(self):
        self.sessions.register(WebAgentIdentity("owner", "project-one", "agent-one",
                                                 AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner", "project-one", "agent-one", "instant", ("instant",)))
        with self.assertRaises(WebReasoningSelectionError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_effort_registration_requires_valid_model_policy(self):
        self.sessions.register(WebAgentIdentity("owner", "project-one", "agent-one",
                                                 AgentRole.EXECUTOR))
        with self.assertRaises(WebModelSelectionError):
            self.models.register_reasoning(TrustedAgentReasoningPolicy(
                "owner", "project-one", "agent-one", "Instant", ("Instant",)))

    def test_registration_rejects_default_not_supported_by_default_model(self):
        self.sessions.register(WebAgentIdentity("owner", "project-one", "agent-one",
                                                 AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner", "project-one", "agent-one", "thinking", ("thinking",)))
        with self.assertRaises(WebReasoningSelectionError):
            self.models.register_reasoning(TrustedAgentReasoningPolicy(
                "owner", "project-one", "agent-one", "Instant", ("Instant",)))

    def test_rebind_policy_refused_and_original_stays_pinned(self):
        self.register()
        with self.assertRaises(WebReasoningSelectionError):
            self.models.register_reasoning(TrustedAgentReasoningPolicy(
                "owner", "project-one", "agent-one", "Medium", ("Medium",)))
        self.models.run(**self.scope(), prompt="original")
        self.assertEqual(self.effort_selector.calls[-1][-1], "Instant")

    def test_wrong_effort_evidence_refused_before_browser(self):
        self.register()
        self.effort_selector.mutator = lambda p: replace(p, reasoning_effort="High")
        with self.assertRaises(WebReasoningSelectionError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_wrong_model_and_every_scope_evidence_field_refused(self):
        self.register()
        for field, value in (("model_id", "thinking"),
                             ("owner_id", "foreign"),
                             ("project_id", "project-two"),
                             ("agent_id", "foreign"),
                             ("chatgpt_project_id", "gpt-two"),
                             ("profile_key", "foreign"),
                             ("mailbox_key", "foreign")):
            self.effort_selector.mutator = lambda p, f=field, v=value: replace(
                p, **{f: v})
            with self.subTest(field=field), self.assertRaises(WebReasoningSelectionError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_false_nonboolean_malformed_and_selector_exception_refused(self):
        self.register()
        for mutate in (lambda p: replace(p, verified=False),
                       lambda p: replace(p, verified=1),
                       lambda p: {}, lambda p: None, lambda p: True,
                       lambda p: (_ for _ in ()).throw(RuntimeError("secret"))):
            self.effort_selector.mutator = mutate
            with self.subTest(mutate=mutate), self.assertRaises(
                    WebReasoningSelectionError) as cm:
                self.models.run(**self.scope(), prompt="blocked")
            self.assertNotIn("secret", str(cm.exception))
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_model_proof_failure_blocks_effort_proof(self):
        self.register()
        self.model_selector.mutator = lambda p: replace(p, verified=False)
        with self.assertRaises(WebModelSelectionError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.effort_selector.calls, [])
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_revoked_project_membership_blocks_both_selectors(self):
        self.register()
        self.member.verified = False
        with self.assertRaises(WebAgentScopeError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.model_selector.calls, [])
        self.assertEqual(self.effort_selector.calls, [])

    def test_effort_proof_is_fresh_each_turn(self):
        self.register()
        self.models.run(**self.scope(), prompt="one")
        self.effort_selector.mutator = lambda p: replace(p, verified=False)
        with self.assertRaises(WebReasoningSelectionError):
            self.models.run(**self.scope(), prompt="two")
        self.assertEqual(len(self.effort_selector.calls), 2)
        self.assertEqual(len(self.ports["agent-one"].calls), 1)

    def test_effort_proof_and_delivery_share_agent_fifo(self):
        self.register()
        entered, release = Event(), Event()
        self.effort_selector.entered = entered
        self.effort_selector.release = release
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.models.run, **self.scope(), prompt="first")
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(self.models.run, **self.scope(), prompt="second")
            time.sleep(0.04)
            self.assertEqual(len(self.effort_selector.calls), 1)
            self.assertEqual(self.ports["agent-one"].calls, [])
            release.set()
            self.assertEqual(first.result(timeout=6).answer, "OK:instant")
            self.assertEqual(second.result(timeout=6).answer, "OK:instant")

    def test_foreign_scope_is_rejected_before_any_proof(self):
        self.register()
        for wrong in (self.scope(owner="foreign"),
                      self.scope(project="project-two"),
                      self.scope(agent="unknown")):
            with self.subTest(wrong=wrong), self.assertRaises(WebAgentScopeError):
                self.models.run(**wrong, prompt="blocked")
        self.assertEqual(self.effort_selector.calls, [])

    def test_duplicate_missing_or_unavailable_catalogs_rejected(self):
        first = self.reasoning_catalogs[0]
        for catalogs in ((), [], (first, first), (object(),),
                         (TrustedProjectReasoningCatalog("unknown", first.model_choices),),
                         (TrustedProjectReasoningCatalog("gpt-one", (
                             TrustedModelReasoningChoices("other", ("Instant",)),)),)):
            with self.subTest(catalogs=catalogs), self.assertRaises(ValueError):
                self.new_registry(reasoning_catalogs=catalogs)
        with self.assertRaises(ValueError):
            self.new_registry(reasoning_selector=None)
        with self.assertRaises(ValueError):
            self.new_registry(reasoning_catalogs=None)
        with self.assertRaises(ValueError):
            self.new_registry(reasoning_selector=object())

    def test_reasoning_catalog_is_immutable(self):
        with self.assertRaises(TypeError):
            self.models._reasoning_projects["gpt-one"] = {}
        with self.assertRaises(TypeError):
            self.models._reasoning_projects["gpt-one"]["instant"] = ("High",)

    def test_legacy_mode_rejects_explicit_effort_and_registration(self):
        legacy = self.new_registry(reasoning_catalogs=None, reasoning_selector=None)
        self.sessions.register(WebAgentIdentity("owner", "project-one", "agent-one",
                                                 AgentRole.EXECUTOR))
        legacy.register(TrustedAgentModelPolicy(
            "owner", "project-one", "agent-one", "instant", ("instant",)))
        with self.assertRaises(WebReasoningSelectionError):
            legacy.register_reasoning(TrustedAgentReasoningPolicy(
                "owner", "project-one", "agent-one", "Instant", ("Instant",)))
        with self.assertRaises(WebReasoningSelectionError):
            legacy.run(**self.scope(), prompt="blocked",
                       requested_reasoning_effort="Instant")
        self.assertEqual(self.ports["agent-one"].calls, [])


if __name__ == "__main__":
    unittest.main()
