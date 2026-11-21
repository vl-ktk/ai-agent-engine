"""WEB-06: selected model must be explicit, verified and never auto-fallback."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import time
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, ModelSelectionEvidence,
    ProjectMembershipEvidence, TrustedAgentModelPolicy,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
    TrustedProjectModelCatalog, WebAgentIdentity, WebAgentModelRegistry,
    WebAgentScopeError, WebAgentSessionRegistry, WebModelSelectionError,
)


class MockMember:
    def __init__(self):
        self.verified = True
        self.calls = []

    def verify(self, *, binding, profile):
        self.calls.append(profile.agent_id)
        return ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, self.verified)


class ModelBrowser:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = []

    def complete(self, *, model_id, messages):
        self.calls.append((model_id, messages[-1].content))
        return BrowserTurn(model_id, "MODEL:" + model_id)


class MockSelector:
    def __init__(self):
        self.calls = []
        self.mutator = None
        self.entered = None
        self.release = None

    def select(self, *, binding, profile, model_id):
        self.calls.append((profile.agent_id, model_id))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            if not self.release.wait(timeout=12):
                raise RuntimeError("mock selection timeout")
        proof = ModelSelectionEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, model_id, True)
        return self.mutator(proof) if self.mutator else proof


class ModelPolicyTests(unittest.TestCase):
    def test_catalog_rejects_invalid_and_duplicate_model_ids(self):
        for models in ((), ("instant", "instant"), ("instant", "bad model"),
                       ("../bad",), ["instant"], (None,)):
            with self.subTest(models=models), self.assertRaises(ValueError):
                TrustedProjectModelCatalog("chatgpt-project", models)
        with self.assertRaises(ValueError):
            TrustedProjectModelCatalog("../project", ("instant",))

    def test_agent_policy_rejects_invalid_or_inconsistent_choices(self):
        for kwargs in (dict(default_model_id="other"),
                       dict(allowed_model_ids=("instant", "instant")),
                       dict(allowed_model_ids=["instant"]),
                       dict(agent_id="../other"),
                       dict(allowed_model_ids=()),
                       dict(default_model_id="instant model")):
            values = dict(owner_id="owner-a", project_id="project-one",
                          agent_id="agent-one", default_model_id="instant",
                          allowed_model_ids=("instant",))
            values.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TrustedAgentModelPolicy(**values)


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.ports = {}
        self.member = MockMember()
        self.selector = MockSelector()

        def factory(profile):
            port = ModelBrowser(profile)
            self.ports[profile.agent_id] = port
            return port

        self.sessions = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant", "thinking", "special")),
            browser_factory=factory,
            project_registry=TrustedChatGPTProjectRegistry((
                TrustedChatGPTProjectBinding("owner-a", "project-one", "gpt-one"),
                TrustedChatGPTProjectBinding("owner-a", "project-two", "gpt-two"))),
            project_verifier=self.member)
        self.models = WebAgentModelRegistry(
            self.sessions,
            project_catalogs=(
                TrustedProjectModelCatalog("gpt-one", ("instant", "thinking")),
                TrustedProjectModelCatalog("gpt-two", ("special",))),
            selector=self.selector)

    def register(self, agent="agent-one", project="project-one",
                 default="instant", allowed=("instant", "thinking")):
        self.sessions.register(WebAgentIdentity("owner-a", project, agent,
                                                AgentRole.EXECUTOR))
        self.models.register(TrustedAgentModelPolicy(
            "owner-a", project, agent, default, allowed))

    def scope(self, agent="agent-one", project="project-one", owner="owner-a"):
        return dict(owner_id=owner, project_id=project, agent_id=agent)

    def test_default_and_explicit_task_model_without_fallback(self):
        self.register()
        self.assertEqual(self.models.run(**self.scope(), prompt="hello").answer,
                         "MODEL:instant")
        self.assertEqual(self.models.run(**self.scope(), prompt="hello",
                                         requested_model_id="thinking").answer,
                         "MODEL:thinking")
        self.assertEqual(self.selector.calls,
                         [("agent-one", "instant"), ("agent-one", "thinking")])
        self.assertEqual([c[0] for c in self.ports["agent-one"].calls],
                         ["instant", "thinking"])

    def test_disallowed_or_unknown_model_refused_before_selector_and_browser(self):
        self.register(allowed=("instant",))
        for model in ("thinking", "special", "nonexistent", "", None, 123, True):
            if model is None:
                # None explicitly means use the registered default.
                continue
            with self.subTest(model=model), self.assertRaises(WebModelSelectionError):
                self.models.run(**self.scope(), prompt="hello",
                                requested_model_id=model)
        self.assertEqual(self.selector.calls, [])
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_two_projects_have_separate_exact_catalogs(self):
        self.register()
        self.register("agent-two", "project-two", "special", ("special",))
        self.assertEqual(self.models.run(**self.scope("agent-two", "project-two"),
                                         prompt="work").answer, "MODEL:special")
        self.assertEqual(self.models.run(**self.scope(), prompt="work").answer,
                         "MODEL:instant")
        with self.assertRaises(WebModelSelectionError):
            self.models.run(**self.scope("agent-two", "project-two"),
                            prompt="wrong", requested_model_id="instant")
        self.assertEqual(self.selector.calls,
                         [("agent-two", "special"), ("agent-one", "instant")])

    def test_registration_fails_if_model_is_not_in_project_catalog(self):
        self.sessions.register(WebAgentIdentity("owner-a", "project-one", "agent-one",
                                                 AgentRole.EXECUTOR))
        with self.assertRaises(WebModelSelectionError):
            self.models.register(TrustedAgentModelPolicy(
                "owner-a", "project-one", "agent-one", "special", ("special",)))
        with self.assertRaises(WebModelSelectionError):
            self.models.run(**self.scope(), prompt="unregistered")

    def test_registration_fails_if_model_not_enabled_in_provider(self):
        self.sessions.register(WebAgentIdentity("owner-a", "project-one", "agent-one",
                                                 AgentRole.EXECUTOR))
        alternate = WebAgentModelRegistry(
            self.sessions,
            project_catalogs=(TrustedProjectModelCatalog("gpt-one", ("instant", "other")),),
            selector=self.selector)
        with self.assertRaises(WebModelSelectionError):
            alternate.register(TrustedAgentModelPolicy(
                "owner-a", "project-one", "agent-one", "other", ("other",)))

    def test_rebinding_registered_agent_policy_is_refused(self):
        self.register()
        with self.assertRaises(WebModelSelectionError):
            self.models.register(TrustedAgentModelPolicy(
                "owner-a", "project-one", "agent-one", "thinking", ("thinking",)))
        self.assertEqual(self.models.selected_model(**self.scope()), "instant")

    def test_foreign_owner_project_and_agent_cannot_choose_or_run(self):
        self.register()
        for bad in (self.scope(owner="owner-b"),
                    self.scope(project="project-two"),
                    self.scope(agent="unknown")):
            with self.subTest(bad=bad):
                with self.assertRaises(WebAgentScopeError):
                    self.models.selected_model(**bad)
                with self.assertRaises(WebAgentScopeError):
                    self.models.run(**bad, prompt="foreign")
        self.assertEqual(self.selector.calls, [])

    def test_wrong_selected_model_is_not_silently_accepted(self):
        self.register()
        self.selector.mutator = lambda p: replace(p, model_id="instant")
        with self.assertRaises(WebModelSelectionError):
            self.models.run(**self.scope(), prompt="thinking",
                            requested_model_id="thinking")
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_proof_cannot_change_project_agent_profile_or_mailbox(self):
        self.register()
        for field, value in (("owner_id", "owner-b"),
                             ("project_id", "project-two"),
                             ("agent_id", "agent-two"),
                             ("chatgpt_project_id", "gpt-two"),
                             ("profile_key", "foreign-profile"),
                             ("mailbox_key", "foreign-mailbox")):
            self.selector.mutator = lambda p, f=field, v=value: replace(p, **{f: v})
            with self.subTest(field=field), self.assertRaises(WebModelSelectionError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_false_nonbool_or_malformed_selector_proof_refused(self):
        self.register()
        for change in (lambda p: replace(p, verified=False),
                       lambda p: replace(p, verified=1),
                       lambda p: None, lambda p: True, lambda p: {}):
            self.selector.mutator = change
            with self.subTest(change=change), self.assertRaises(WebModelSelectionError):
                self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_selector_exception_is_sanitized_and_no_browser_call(self):
        self.register()

        def fail(_):
            raise RuntimeError("private model secret")

        self.selector.mutator = fail
        with self.assertRaises(WebModelSelectionError) as cm:
            self.models.run(**self.scope(), prompt="blocked")
        self.assertNotIn("secret", str(cm.exception))
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_membership_revocation_preempts_model_selector(self):
        self.register()
        self.member.verified = False
        with self.assertRaises(WebAgentScopeError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(self.selector.calls, [])
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_selector_verified_every_turn_and_no_fallback_after_failure(self):
        self.register()
        self.models.run(**self.scope(), prompt="first")
        self.selector.mutator = lambda p: replace(p, verified=False)
        with self.assertRaises(WebModelSelectionError):
            self.models.run(**self.scope(), prompt="blocked")
        self.assertEqual(len(self.selector.calls), 2)
        self.assertEqual(len(self.ports["agent-one"].calls), 1)

    def test_model_selection_and_send_share_agent_fifo(self):
        self.register()
        entered, release = Event(), Event()
        self.selector.entered, self.selector.release = entered, release
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.models.run, **self.scope(), prompt="first")
            self.assertTrue(entered.wait(timeout=5))
            other = pool.submit(self.models.run, **self.scope(), prompt="second")
            time.sleep(0.04)
            self.assertEqual(self.selector.calls, [("agent-one", "instant")])
            self.assertEqual(self.ports["agent-one"].calls, [])
            release.set()
            self.assertEqual(first.result(timeout=6).answer, "MODEL:instant")
            self.assertEqual(other.result(timeout=6).answer, "MODEL:instant")
        self.assertEqual(len(self.ports["agent-one"].calls), 2)

    def test_legacy_registry_without_project_binding_cannot_register_models(self):
        legacy = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("instant",)),
            browser_factory=lambda profile: ModelBrowser(profile))
        legacy.register(WebAgentIdentity("owner-a", "project-one", "agent-one",
                                         AgentRole.EXECUTOR))
        registry = WebAgentModelRegistry(
            legacy, project_catalogs=(TrustedProjectModelCatalog("gpt-one", ("instant",)),),
            selector=self.selector)
        with self.assertRaises(WebAgentScopeError):
            registry.register(TrustedAgentModelPolicy(
                "owner-a", "project-one", "agent-one", "instant", ("instant",)))

    def test_registry_constructor_rejects_missing_duplicate_or_bad_catalogs(self):
        for catalogs in ((), [],
                         (TrustedProjectModelCatalog("gpt-one", ("instant",)),) * 2,
                         (object(),)):
            with self.subTest(catalogs=catalogs), self.assertRaises(ValueError):
                WebAgentModelRegistry(self.sessions, project_catalogs=catalogs,
                                      selector=self.selector)
        with self.assertRaises(ValueError):
            WebAgentModelRegistry(self.sessions,
                                  project_catalogs=(TrustedProjectModelCatalog(
                                      "gpt-one", ("instant",)),),
                                  selector=object())
        with self.assertRaises(TypeError):
            self.models._projects["gpt-one"] = TrustedProjectModelCatalog(
                "gpt-one", ("special",))


if __name__ == "__main__":
    unittest.main()
