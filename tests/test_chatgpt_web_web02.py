"""WEB-02 offline tests: fake BrowserPort only, never a live ChatGPT session."""

import unittest

from ai_agent_engine import (
    AgentRole, Message, ModelRouter, ModelSetupError, ProviderSpec,
    SessionContext, TextOnlyModel, Tool,
)
from ai_agent_engine.chatgpt_web import (
    BrowserTurn, ChatGPTWebConfig, ChatGPTWebModel, ChatGPTWebUnavailable,
    chatgpt_web_provider_spec,
)


def context():
    return SessionContext("owner-project", "worker-web02", AgentRole.EXECUTOR,
                          "One synthetic task")


class FakeBrowser:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or BrowserTurn("gpt-fake", "mock-only response")
        self.error = error

    def complete(self, *, model_id, messages):
        self.calls.append((model_id, messages))
        if self.error is not None:
            raise self.error
        return self.result


class Web02ConfigTests(unittest.TestCase):
    def test_off_by_default_and_never_hides_an_implicit_model(self):
        config = ChatGPTWebConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(config.models, ())
        with self.assertRaises(ValueError):
            chatgpt_web_provider_spec(config)
        with self.assertRaises(ValueError):
            ChatGPTWebConfig(enabled=True)

    def test_rejects_invalid_switch_model_catalog_and_reply_limit(self):
        for kwargs in (
            {"enabled": 1},
            {"enabled": "true"},
            {"models": ["gpt-fake"]},
            {"models": ("gpt-fake", "gpt-fake")},
            {"models": ("model with spaces",)},
            {"models": ("../../bad",)},
            {"models": (42,)},
            {"max_reply_chars": 0},
            {"max_reply_chars": 20_001},
            {"max_reply_chars": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ChatGPTWebConfig(**kwargs)

    def test_bad_factory_never_registers(self):
        with self.assertRaises(ValueError):
            chatgpt_web_provider_spec(ChatGPTWebConfig(models=("gpt-fake",)),
                                      browser_factory="not callable")


class Web02ProviderTests(unittest.TestCase):
    def test_disabled_route_rejects_without_constructing_browser(self):
        factory_calls = []

        def browser_factory(model_id):
            factory_calls.append(model_id)
            return FakeBrowser()

        spec = chatgpt_web_provider_spec(
            ChatGPTWebConfig(models=("gpt-fake",)),
            browser_factory=browser_factory)
        self.assertEqual(spec.provider, "chatgpt_web")
        self.assertFalse(spec.supports_tools)
        router = ModelRouter((spec,))
        self.assertEqual(router.available, (("chatgpt_web", "gpt-fake"),))
        with self.assertRaises(ModelSetupError) as error:
            router.new_session(provider="chatgpt_web", model_id="gpt-fake",
                               context=context())
        self.assertNotIn("browser", str(error.exception))
        self.assertEqual(factory_calls, [])

    def test_injected_fake_works_through_existing_model_router(self):
        fake = FakeBrowser()
        built = []
        config = ChatGPTWebConfig(enabled=True, models=("gpt-fake",))

        def factory(model_id):
            built.append(model_id)
            return fake

        router = ModelRouter((
            chatgpt_web_provider_spec(config, browser_factory=factory),
            ProviderSpec("local_stub", ("fallback",),
                         lambda _model: TextOnlyModel(lambda _h: "unrelated")),
        ))
        self.assertEqual(built, [])
        session = router.new_session(provider="chatgpt_web", model_id="gpt-fake",
                                     context=context())
        self.assertEqual(built, ["gpt-fake"])
        result = session.run("WEB02 only")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.answer, "mock-only response")
        self.assertEqual(len(fake.calls), 1)
        requested_model, messages = fake.calls[0]
        self.assertEqual(requested_model, "gpt-fake")
        self.assertEqual(tuple(message.role for message in messages),
                         ("system", "user"))
        self.assertEqual(messages[-1].content, "WEB02 only")
        self.assertEqual(router.new_session(provider="local_stub",
                                            model_id="fallback",
                                            context=context()).run("still works").answer,
                         "unrelated")

    def test_unknown_route_and_tools_refused_before_browser_factory(self):
        calls = []
        spec = chatgpt_web_provider_spec(
            ChatGPTWebConfig(enabled=True, models=("gpt-fake",)),
            browser_factory=lambda name: calls.append(name) or FakeBrowser())
        router = ModelRouter((spec,))
        with self.assertRaises(ValueError):
            router.new_session(provider="chatgpt_web", model_id="gpt-hidden",
                               context=context())
        with self.assertRaises(ValueError):
            router.new_session(provider="chatgpt_web", model_id="gpt-fake",
                               context=context(), tools=(Tool("execute", lambda args: "x"),))
        self.assertEqual(calls, [])

    def test_no_browser_and_unusable_browser_are_fail_closed(self):
        config = ChatGPTWebConfig(enabled=True, models=("gpt-fake",))
        for factory in (None, lambda _name: object(), lambda _name: None):
            with self.subTest(factory=factory):
                router = ModelRouter((chatgpt_web_provider_spec(
                    config, browser_factory=factory),))
                with self.assertRaises(ModelSetupError):
                    router.new_session(provider="chatgpt_web", model_id="gpt-fake",
                                       context=context())

    def test_factory_error_is_redacted_and_not_retried(self):
        calls = []

        def broken(model_id):
            calls.append(model_id)
            raise RuntimeError("private-secret-in-browser-profile")

        router = ModelRouter((chatgpt_web_provider_spec(
            ChatGPTWebConfig(enabled=True, models=("gpt-fake",)),
            browser_factory=broken),))
        with self.assertRaises(ModelSetupError) as error:
            router.new_session(provider="chatgpt_web", model_id="gpt-fake",
                               context=context())
        self.assertNotIn("private-secret", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assertEqual(calls, ["gpt-fake"])


class Web02TurnTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBrowser()
        self.config = ChatGPTWebConfig(enabled=True, models=("gpt-fake",))
        self.model = ChatGPTWebModel(self.config, "gpt-fake", self.fake)

    def test_direct_disabled_model_construction_fails(self):
        with self.assertRaises(ChatGPTWebUnavailable):
            ChatGPTWebModel(ChatGPTWebConfig(models=("gpt-fake",)),
                            "gpt-fake", self.fake)
        self.assertEqual(self.fake.calls, [])

    def test_invalid_history_or_tool_request_never_calls_driver(self):
        cases = (
            ((), ()),
            ((Message("tool", "x"),), ()),
            ((Message("user", "x"), Message("user", "y")), ()),
            ((Message("system", "x"),), ()),
            ((Message("user", ""),), ()),
            ((Message("user", "\x00"),), ()),
            ((Message("user", "x" * 65537),), ()),
            ((Message("user", "x"),), ("execute",)),
        )
        for history, tools in cases:
            with self.subTest(history=history, tools=tools):
                with self.assertRaises(ChatGPTWebUnavailable):
                    self.model.complete(history, tools)
        self.assertEqual(self.fake.calls, [])

    def test_wrong_model_empty_or_oversize_result_does_not_fallback(self):
        for result in (
            BrowserTurn("other-model", "looks valid"),
            BrowserTurn("gpt-fake", ""),
            BrowserTurn("gpt-fake", "contains\x00nul"),
            BrowserTurn("gpt-fake", "x" * 20_001),
            "not a BrowserTurn",
        ):
            with self.subTest(result=result.__class__.__name__):
                self.fake.result = result
                with self.assertRaises(ChatGPTWebUnavailable):
                    self.model.complete((Message("user", "work"),), ())

    def test_unknown_send_failure_is_sanitized_and_not_retried(self):
        self.fake.error = RuntimeError("private-secret-after-send")
        with self.assertRaises(ChatGPTWebUnavailable) as error:
            self.model.complete((Message("user", "work"),), ())
        self.assertEqual(str(error.exception), "web browser operation failed")
        self.assertNotIn("private-secret", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assertEqual(len(self.fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
