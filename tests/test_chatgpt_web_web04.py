"""WEB-04: negative project-membership and cross-project tests (no real UI)."""

from dataclasses import replace
import unittest

from ai_agent_engine import (
    AgentRole, BrowserTurn, ChatGPTWebConfig, ProjectMembershipEvidence,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
    WebAgentIdentity, WebAgentScopeError, WebAgentSessionRegistry,
)


def binding(owner="owner-a", project="internal-one", external="gpt-project-one"):
    return TrustedChatGPTProjectBinding(owner, project, external)


class MockBrowser:
    def __init__(self, profile):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = []

    def complete(self, *, model_id, messages):
        self.calls.append((model_id, messages))
        return BrowserTurn(model_id, "MOCK:" + messages[-1].content)


class MockVerifier:
    def __init__(self):
        self.calls = []
        self.mutator = None

    def verify(self, *, binding, profile):
        self.calls.append((binding, profile))
        evidence = ProjectMembershipEvidence(
            binding.owner_id, binding.project_id, profile.agent_id,
            binding.chatgpt_project_id, profile.profile_key,
            profile.mailbox_key, True)
        if self.mutator is not None:
            return self.mutator(evidence)
        return evidence


class TrustedBindingTests(unittest.TestCase):
    def test_invalid_ids_refused(self):
        for kwargs in ({"owner": ""}, {"project": "../other"},
                       {"external": "https://chatgpt.com/g/project"},
                       {"external": "external project"},
                       {"external": "a" * 129}, {"external": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                binding(**kwargs)

    def test_registry_rejects_missing_or_ambiguous_configuration(self):
        for bindings in ((), [binding()], (binding(), binding()),
                         (binding(), binding(project="internal-two")),
                         (binding(), binding(owner="owner-b"))):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                TrustedChatGPTProjectRegistry(bindings)

    def test_two_external_projects_can_coexist_but_are_scoped(self):
        projects = TrustedChatGPTProjectRegistry((
            binding(), binding(project="internal-two", external="gpt-project-two")))
        self.assertEqual(projects.binding_for(owner_id="owner-a",
                                               project_id="internal-two"),
                         binding(project="internal-two", external="gpt-project-two"))
        with self.assertRaises(TypeError):
            projects._bindings[("owner-a", "internal-one")] = binding(
                external="silently-rebound")
        for owner, project in (("owner-b", "internal-one"),
                               ("owner-a", "internal-three"),
                               ("../owner", "internal-one")):
            with self.subTest(owner=owner, project=project), self.assertRaises(
                    WebAgentScopeError):
                projects.binding_for(owner_id=owner, project_id=project)


class MembershipGateTests(unittest.TestCase):
    def setUp(self):
        self.ports = {}
        self.verifier = MockVerifier()
        self.projects = TrustedChatGPTProjectRegistry((
            binding(), binding(project="internal-two", external="gpt-project-two")))

        def factory(profile):
            port = MockBrowser(profile)
            self.ports[profile.agent_id] = port
            return port

        self.factory = factory
        self.registry = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("synthetic",)),
            browser_factory=factory, project_registry=self.projects,
            project_verifier=self.verifier)

    def register(self, agent="agent-one", project="internal-one", owner="owner-a"):
        return self.registry.register(WebAgentIdentity(
            owner, project, agent, AgentRole.EXECUTOR, "Safe instructions"))

    def run_agent(self, agent="agent-one", project="internal-one", owner="owner-a"):
        return self.registry.run(owner_id=owner, project_id=project,
                                 agent_id=agent, model_id="synthetic", prompt="hello")

    def test_project_binding_is_trusted_and_exposed_only_to_own_agent(self):
        self.register()
        self.assertEqual(self.registry.project_binding(
            owner_id="owner-a", project_id="internal-one", agent_id="agent-one"),
            binding())
        with self.assertRaises(WebAgentScopeError):
            self.registry.project_binding(owner_id="owner-b", project_id="internal-one",
                                          agent_id="agent-one")

    def test_two_agents_in_separate_chatgpt_projects(self):
        self.register()
        self.register(agent="agent-two", project="internal-two")
        self.assertEqual(self.run_agent().answer, "MOCK:hello")
        self.assertEqual(self.run_agent(agent="agent-two", project="internal-two").answer,
                         "MOCK:hello")
        self.assertEqual([x[0].chatgpt_project_id for x in self.verifier.calls],
                         ["gpt-project-one", "gpt-project-two"])
        self.assertEqual(len(self.ports["agent-one"].calls), 1)
        self.assertEqual(len(self.ports["agent-two"].calls), 1)

    def test_unknown_project_refused_before_browser_port_creation(self):
        with self.assertRaises(WebAgentScopeError):
            self.register(project="untrusted")
        with self.assertRaises(WebAgentScopeError):
            self.register(owner="owner-b")
        self.assertEqual(self.ports, {})

    def test_foreign_agent_scope_refused_before_verifier_or_send(self):
        self.register()
        for owner, project, agent in (("owner-b", "internal-one", "agent-one"),
                                      ("owner-a", "internal-two", "agent-one"),
                                      ("owner-a", "internal-one", "agent-two")):
            with self.subTest(owner=owner, project=project, agent=agent):
                with self.assertRaises(WebAgentScopeError):
                    self.run_agent(owner=owner, project=project, agent=agent)
        self.assertEqual(self.verifier.calls, [])
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_foreign_chatgpt_project_claim_refused(self):
        self.register()
        self.verifier.mutator = lambda e: replace(e, chatgpt_project_id="gpt-project-two")
        with self.assertRaises(WebAgentScopeError):
            self.run_agent()
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_verifier_cannot_change_owner_project_or_agent(self):
        self.register()
        for field, value in (("owner_id", "owner-b"),
                             ("project_id", "internal-two"),
                             ("agent_id", "agent-two"),
                             ("profile_key", "profile-wrong"),
                             ("mailbox_key", "mailbox-wrong")):
            with self.subTest(field=field), self.assertRaises(WebAgentScopeError):
                self.verifier.mutator = lambda e, f=field, v=value: replace(e, **{f: v})
                self.run_agent()
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_unverified_false_nonbool_and_wrong_type_refused(self):
        self.register()
        for evidence in (lambda e: replace(e, verified=False),
                         lambda e: replace(e, verified=1),
                         lambda e: None, lambda e: True,
                         lambda e: {"verified": True}):
            with self.subTest(evidence=evidence), self.assertRaises(
                    WebAgentScopeError):
                self.verifier.mutator = evidence
                self.run_agent()
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_verifier_exceptions_sanitized_and_no_browser_turn(self):
        self.register()

        def fail(_):
            raise RuntimeError("private mock verifier secret")

        self.verifier.mutator = fail
        with self.assertRaises(WebAgentScopeError) as error:
            self.run_agent()
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.ports["agent-one"].calls, [])

    def test_each_turn_rechecks_membership_and_revocation_is_immediate(self):
        self.register()
        self.assertEqual(self.run_agent().answer, "MOCK:hello")
        self.verifier.mutator = lambda e: replace(e, verified=False)
        with self.assertRaises(WebAgentScopeError):
            self.run_agent()
        self.assertEqual(len(self.verifier.calls), 2)
        self.assertEqual(len(self.ports["agent-one"].calls), 1)
        self.assertEqual(self.registry.pending(owner_id="owner-a",
                                               project_id="internal-one",
                                               agent_id="agent-one"), 0)
        self.verifier.mutator = None
        self.assertEqual(self.run_agent().answer, "MOCK:hello")

    def test_incomplete_or_untrusted_gate_configuration_refused(self):
        config = ChatGPTWebConfig(enabled=True, models=("synthetic",))
        for kwargs in ({"project_registry": self.projects},
                       {"project_verifier": self.verifier},
                       {"project_registry": object(),
                        "project_verifier": self.verifier},
                       {"project_registry": self.projects,
                        "project_verifier": object()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                WebAgentSessionRegistry(config, browser_factory=self.factory, **kwargs)

    def test_unbound_web03_mode_does_not_claim_trusted_project(self):
        legacy = WebAgentSessionRegistry(
            ChatGPTWebConfig(enabled=True, models=("synthetic",)),
            browser_factory=self.factory)
        legacy.register(WebAgentIdentity("owner-a", "internal-one", "agent-one",
                                         AgentRole.EXECUTOR))
        with self.assertRaises(WebAgentScopeError):
            legacy.project_binding(owner_id="owner-a", project_id="internal-one",
                                   agent_id="agent-one")


if __name__ == "__main__":
    unittest.main()
