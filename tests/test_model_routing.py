"""Offline Stage 3.5 model routing tests; never invoke live providers."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from ai_agent_engine import (
    Action, AgentRole, Message, ModelRouter, ModelTurn, ProviderSpec,
    SandboxFiles, ScriptedModel, SessionContext, TextOnlyModel, Tool, ToolRegistry,
)
from ai_agent_engine.plan_agent import OpenAIPlanModel
from ai_agent_engine.siwc import CredentialStore


def context(role=AgentRole.EXECUTOR):
    return SessionContext("owner-project", "worker-a", role, "One authorized task")


class ProviderRegistryTests(unittest.TestCase):
    def test_multiple_providers_share_same_session_contract_and_isolated_context(self):
        invoked = []

        def factory(provider):
            def build(model_id):
                invoked.append(("build", provider, model_id))

                def respond(history):
                    invoked.append(("invoke", provider, tuple(m.role for m in history)))
                    return provider + ":" + model_id

                return TextOnlyModel(respond)
            return build

        router = ModelRouter((
            ProviderSpec("openai", ("gpt-test", "gpt-other"), factory("openai")),
            ProviderSpec("anthropic", ("claude-test",), factory("anthropic")),
            ProviderSpec("local", ("offline-stub",), factory("local")),
        ))
        self.assertEqual(router.available, (
            ("openai", "gpt-test"), ("openai", "gpt-other"),
            ("anthropic", "claude-test"), ("local", "offline-stub"),
        ))
        for provider, model_id in router.available:
            with self.subTest(provider=provider, model_id=model_id):
                result = router.new_session(provider=provider, model_id=model_id,
                                            context=context()).run("work")
                self.assertEqual(result.status, "done")
                self.assertEqual(result.answer, provider + ":" + model_id)
                self.assertEqual(result.audit[0].project_id, "owner-project")
        self.assertEqual(len([item for item in invoked if item[0] == "invoke"]), 4)
        self.assertTrue(all(item[2] == ("system", "user")
                            for item in invoked if item[0] == "invoke"))

    def test_unknown_routes_fail_without_invoking_provider_or_falling_back(self):
        created = []
        router = ModelRouter((
            ProviderSpec("openai", ("gpt-approved",),
                         lambda name: created.append(name) or TextOnlyModel(lambda h: "ok")),
        ))
        for provider, name in (("anthropic", "claude"), ("openai", "gpt-hidden"),
                               ("openai", ""), ("openai", "../../secret")):
            with self.subTest(provider=provider, name=name), self.assertRaises(ValueError):
                router.new_session(provider=provider, model_id=name, context=context())
        self.assertEqual(created, [])

    def test_tool_incompatible_provider_fails_before_factory_invocation(self):
        touched = []
        router = ModelRouter((
            ProviderSpec("anthropic", ("claude-text",),
                         lambda name: touched.append(name) or TextOnlyModel(lambda h: "ok")),
        ))
        read = Tool("read_file", lambda args: "ok")
        for kwargs in ({"tools": (read,)},
                       {"registry": ToolRegistry((read,), allowed=("read_file",))}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                router.new_session(provider="anthropic", model_id="claude-text",
                                   context=context(), **kwargs)
        self.assertEqual(touched, [])

    def test_empty_tool_grants_do_not_require_tool_capability(self):
        read = Tool("read_file", lambda args: "ok")
        router = ModelRouter((ProviderSpec("anthropic", ("text",),
                                           lambda name: TextOnlyModel(lambda h: "ok")),))
        session = router.new_session(provider="anthropic", model_id="text",
                                     context=context(),
                                     registry=ToolRegistry((read,), allowed=()))
        self.assertEqual(session.run("task").status, "done")

    def test_tool_capable_provider_still_uses_exact_session_grants(self):
        invoked = []

        def model_factory(model_id):
            self.assertEqual(model_id, "gpt-tool")

            class FakeModel:
                def __init__(self):
                    self.step = 0

                def complete(self, history, allowed_tools):
                    self.step += 1
                    invoked.append(allowed_tools)
                    if self.step == 1:
                        return ModelTurn(action=Action("read_file", {}))
                    return ModelTurn(final_text="done")

            return FakeModel()

        router = ModelRouter((ProviderSpec("openai", ("gpt-tool",), model_factory,
                                            supports_tools=True),))
        tools = (Tool("read_file", lambda args: "safe"),
                 Tool("write_new_file", lambda args: "should not run"))
        result = router.new_session(provider="openai", model_id="gpt-tool",
                                    context=context(AgentRole.SUPERVISOR),
                                    registry=ToolRegistry(tools, allowed=("read_file",))).run("test")
        self.assertEqual(result.status, "done")
        self.assertEqual(invoked, [("read_file",), ("read_file",)])
        self.assertEqual([item.event for item in result.audit], ["tool_ok", "completed"])

    def test_reject_duplicate_providers_invalid_models_and_factories(self):
        valid = ProviderSpec("openai", ("gpt-6.1-sol",), lambda name: ScriptedModel(()))
        with self.assertRaises(ValueError):
            ModelRouter((valid, valid))
        for kwargs in (
            {"provider": "../../openai"},
            {"provider": "OpenAI"},
            {"models": ()},
            {"models": ("m", "m")},
            {"models": ("invalid model",)},
            {"models": ("../../../auth",)},
            {"supports_tools": "yes"},
            {"factory": None},
        ):
            base = {"provider": "openai", "models": ("gpt-test",),
                    "factory": lambda name: TextOnlyModel(lambda h: "ok")}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ProviderSpec(**(base | kwargs))

    def test_factory_must_return_model_protocol_implementation(self):
        router = ModelRouter((ProviderSpec("broken", ("bad",), lambda name: object()),))
        with self.assertRaises(TypeError):
            router.build_model("broken", "bad")

    def test_router_is_lazy_and_keeps_one_session_single_use(self):
        built = []
        router = ModelRouter((ProviderSpec("local", ("stub",),
                                           lambda name: built.append(name) or
                                           TextOnlyModel(lambda h: "ok")),))
        self.assertEqual(built, [])
        self.assertEqual(router.available, (("local", "stub"),))
        self.assertEqual(built, [])
        session = router.new_session(provider="local", model_id="stub", context=context())
        self.assertEqual(built, ["stub"])
        self.assertEqual(session.run("task").status, "done")
        with self.assertRaises(RuntimeError):
            session.run("repeat")

    def test_invalid_step_cap_does_not_even_construct_provider(self):
        built = []
        router = ModelRouter((ProviderSpec("openai", ("gpt",),
                                           lambda name: built.append(name) or
                                           ScriptedModel(())),))
        for count in (0, 21, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                router.new_session(provider="openai", model_id="gpt",
                                   context=context(), max_steps=count)
        self.assertEqual(built, [])


class TextOnlyModelTests(unittest.TestCase):
    def test_invalid_history_or_tools_never_invokes_callback(self):
        calls = []
        model = TextOnlyModel(lambda history: calls.append(history) or "ok")
        invalid = (
            ((Message("user", "test"),), ("read_file",)),
            ((), ()),
            ((Message("tool", "secret"),), ()),
            ((Message("system", "instructions"), Message("tool", "data")), ()),
            ((Message("system", "a"), Message("user", "b"), Message("user", "c")), ()),
        )
        for history, tools in invalid:
            with self.subTest(history=history, tools=tools), self.assertRaises(ValueError):
                model.complete(history, tools)
        self.assertEqual(calls, [])

    def test_malformed_and_oversized_text_response_is_rejected(self):
        for response in (None, 42, "", " ", "abcde"):
            with self.subTest(response=response):
                model = TextOnlyModel(lambda history: response, max_reply_chars=4)
                with self.assertRaises(ValueError):
                    model.complete((Message("user", "task"),), ())

    def test_model_callback_failure_is_redacted_by_shared_runtime(self):
        def fails(history):
            raise RuntimeError("provider-private-token")

        router = ModelRouter((ProviderSpec("anthropic", ("claude",),
                                           lambda name: TextOnlyModel(fails)),))
        result = router.new_session(provider="anthropic", model_id="claude",
                                    context=context()).run("test")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.audit[0].event, "model_error")
        self.assertNotIn("provider-private-token", repr(result.audit))


class ExistingGPTAdapterCompatibilityTests(unittest.TestCase):
    def test_router_can_select_actual_gpt_adapter_with_synthetic_transport(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = CredentialStore(root / "private")
            store.save_session({
                "subject": "owner", "client_id": "oaiapp_test",
                "access_token": "private-synthetic-access",
                "refresh_token": "private-synthetic-refresh",
                "sharing_enabled": True,
                "scopes": ["openid", "resource.invoke", "chatgpt.tokens.use.direct"],
                "expires_at": time.time() + 3600,
            })
            calls = []

            def stream(items):
                message = {"type": "response.completed", "response": {
                    "status": "completed", "output": items,
                }}
                return BytesIO(b"data: " + json.dumps(message).encode("utf-8") +
                               b"\n\ndata: [DONE]\n\n")

            outputs = iter((
                stream([{"type": "function_call", "name": "sandbox.write_new_file",
                         "call_id": "call1", "arguments": json.dumps({
                             "path": "test.txt", "content": "safe",
                         })}]),
                stream([{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "completed"},
                ]}]),
            ))

            def fake_transport(request, timeout):
                calls.append(json.loads(request.data))
                return next(outputs)

            router = ModelRouter((
                ProviderSpec("openai", ("gpt-test",),
                             lambda name: OpenAIPlanModel(name, store, opener=fake_transport),
                             supports_tools=True),
                ProviderSpec("anthropic", ("claude-text-mock",),
                             lambda name: TextOnlyModel(lambda history: "claude stub only")),
            ))
            scratch = root / "sandbox"
            scratch.mkdir()
            result = router.new_session(
                provider="openai", model_id="gpt-test", context=context(),
                tools=SandboxFiles(scratch).allowed_tools(),
            ).run("Create test.txt")
            self.assertEqual(result.status, "done")
            self.assertEqual(result.answer, "completed")
            self.assertEqual((scratch / "test.txt").read_text(), "safe")
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(request["store"] is False and request["stream"] is True
                                for request in calls))
            self.assertEqual(router.new_session(
                provider="anthropic", model_id="claude-text-mock", context=context(),
            ).run("Another task").answer, "claude stub only")


if __name__ == "__main__":
    unittest.main()
