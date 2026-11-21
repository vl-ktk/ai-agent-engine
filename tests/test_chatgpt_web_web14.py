"""WEB-14: strict BrowserPort fault injection, sticky HOLD, no bypass/retry."""

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

from ai_agent_engine.chatgpt_web import (
    BrowserTurn, ChatGPTWebConfig, ChatGPTWebModel, ChatGPTWebUnavailable,
)
from ai_agent_engine.chatgpt_web_failures import (
    GuardedBrowserPort, WebFaultCode, WebFaultGuard, WebFaultHold,
    WebPortFault,
)
from ai_agent_engine.chatgpt_web_sessions import WebAgentProfile
from ai_agent_engine.core import Message

if sys.platform.startswith("linux"):
    import test_chatgpt_web_web12 as web12_fixtures
    from ai_agent_engine.chatgpt_web_delivery import (
        SQLiteWebDeliveryJournal, WebDeliveryHold, WebIdempotentDispatcher,
    )
    from ai_agent_engine.task_delivery import TaskDeliveryManager
    from ai_agent_engine.task_state import TaskState
    from test_main_specialists import OWNER
    from pathlib import Path


PROFILE = WebAgentProfile("vlady", "engine", "ui_one", "private-profile", "mailbox-one")


class FaultyBrowser:
    def __init__(self, profile=PROFILE, *, fault=None, result=None):
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self.calls = 0
        self.fault = fault
        self.result = result or BrowserTurn("instant", "Mock response")
        self.security_bypasses = 0

    def complete(self, *, model_id, messages):
        self.calls += 1
        if self.fault is not None:
            raise self.fault
        return self.result

    def solve_captcha(self):
        self.security_bypasses += 1

    def login_again(self):
        self.security_bypasses += 1

    def switch_model(self):
        self.security_bypasses += 1


class FaultGuardContractTests(unittest.TestCase):
    def wrap(self, browser):
        guard = WebFaultGuard(PROFILE)
        port = GuardedBrowserPort(PROFILE, browser, guard)
        model = ChatGPTWebModel(ChatGPTWebConfig(
            enabled=True, models=("instant",)), "instant", port)
        return guard, port, model

    def test_every_explicit_fault_latches_hold_and_never_retries(self):
        for code in WebFaultCode:
            with self.subTest(code=code.value):
                port_error = WebPortFault(
                    code, retry_after_seconds=60
                    if code is WebFaultCode.RATE_LIMITED else None)
                browser = FaultyBrowser(fault=port_error)
                guard, port, model = self.wrap(browser)
                for attempt in (1, 2, 3):
                    with self.assertRaises(ChatGPTWebUnavailable) as error:
                        model.complete((Message("user", "safe mock"),), ())
                    self.assertNotIn(code.value, str(error.exception))
                self.assertEqual(browser.calls, 1)
                self.assertEqual(browser.security_bypasses, 0)
                state = guard.snapshot()
                self.assertEqual((state.state, state.code), ("HOLD", code))
                self.assertIsNotNone(state.observed_at)
                self.assertEqual(state.retry_after_seconds,
                                 60 if code is WebFaultCode.RATE_LIMITED else None)
                self.assertFalse(hasattr(port, "solve_captcha"))
                self.assertFalse(hasattr(port, "login_again"))
                self.assertFalse(hasattr(port, "switch_model"))

    def test_untyped_browser_exception_always_unknown_and_sanitized(self):
        browser = FaultyBrowser(fault=RuntimeError(
            "CAPTCHA private-secret after UI click"))
        guard, port, model = self.wrap(browser)
        with self.assertRaises(ChatGPTWebUnavailable) as error:
            model.complete((Message("user", "hi"),), ())
        self.assertEqual(str(error.exception), "web browser operation failed")
        self.assertIsNone(error.exception.__cause__)
        self.assertEqual(guard.snapshot().code, WebFaultCode.UNKNOWN_SEND)
        self.assertNotIn("private-secret", str(guard.snapshot()))
        with self.assertRaises(WebFaultHold):
            port.complete(model_id="instant", messages=(Message("user", "hi"),))
        self.assertEqual(browser.calls, 1)

    def test_mutated_or_subclassed_fault_cannot_disable_hold(self):
        class ForgedPortFault(WebPortFault):
            pass

        forged = ForgedPortFault(WebFaultCode.CAPTCHA)
        tampered = WebPortFault(WebFaultCode.RATE_LIMITED)
        tampered.code = "unsupported_from_page"
        for fault in (forged, tampered):
            with self.subTest(fault=type(fault).__name__):
                browser = FaultyBrowser(fault=fault)
                guard, _, model = self.wrap(browser)
                with self.assertRaises(ChatGPTWebUnavailable):
                    model.complete((Message("user", "safe"),), ())
                self.assertEqual(guard.snapshot().code, WebFaultCode.UNKNOWN_SEND)
                with self.assertRaises(ChatGPTWebUnavailable):
                    model.complete((Message("user", "safe"),), ())
                self.assertEqual(browser.calls, 1)

    def test_concurrent_fault_invocations_are_stopped_after_first(self):
        browser = FaultyBrowser(fault=WebPortFault(WebFaultCode.CAPTCHA))
        guard, _, model = self.wrap(browser)
        def call():
            try:
                model.complete((Message("user", "safe"),), ())
            except ChatGPTWebUnavailable:
                return "held"
            return "unexpected success"
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(lambda _: call(), range(20))), ["held"] * 20)
        self.assertEqual(browser.calls, 1)
        self.assertEqual(guard.snapshot().code, WebFaultCode.CAPTCHA)

    def test_safety_hold_during_valid_browser_reply_still_blocks_result(self):
        browser = FaultyBrowser()
        guard, _, model = self.wrap(browser)
        def latch_then_reply(*, model_id, messages):
            browser.calls += 1
            guard._hold(WebFaultCode.ANTI_BOT)
            return BrowserTurn(model_id, "looks acceptable")
        browser.complete = latch_then_reply
        with self.assertRaises(ChatGPTWebUnavailable):
            model.complete((Message("user", "safe"),), ())
        self.assertEqual(guard.snapshot().code, WebFaultCode.ANTI_BOT)
        self.assertEqual(browser.calls, 1)

    def test_invalid_response_and_model_mismatch_hold_without_fallback(self):
        for reply, code in (
            (BrowserTurn("unsupported", "Some text"), WebFaultCode.WRONG_MODEL),
            (BrowserTurn("instant", ""), WebFaultCode.INVALID_RESPONSE),
            (BrowserTurn("instant", "contains\x00nul"), WebFaultCode.INVALID_RESPONSE),
            (BrowserTurn("instant", "x" * 20_001), WebFaultCode.INVALID_RESPONSE),
            ({"text": "not trustworthy"}, WebFaultCode.INVALID_RESPONSE),
        ):
            with self.subTest(code=code, reply_type=type(reply).__name__):
                browser = FaultyBrowser(result=reply)
                browser.result = reply
                guard, _, model = self.wrap(browser)
                with self.assertRaises(ChatGPTWebUnavailable):
                    model.complete((Message("user", "safe mock"),), ())
                self.assertEqual(guard.snapshot().code, code)
                self.assertEqual(browser.calls, 1)

    def test_rejects_arbitrary_codes_and_untrusted_retry_after(self):
        for code, delay in (
            ("captcha", None), (WebFaultCode.CAPTCHA, 30),
            (WebFaultCode.RATE_LIMITED, -1),
            (WebFaultCode.RATE_LIMITED, 86_401),
            (WebFaultCode.RATE_LIMITED, "infinite"),
            (WebFaultCode.RATE_LIMITED, True),
        ):
            with self.subTest(code=code, delay=delay), self.assertRaises(ValueError):
                WebPortFault(code, retry_after_seconds=delay)

    def test_scope_and_exclusive_binding_enforced(self):
        guard, port, _ = self.wrap(FaultyBrowser())
        with self.assertRaises(ValueError):
            GuardedBrowserPort(PROFILE, FaultyBrowser(), guard)
        foreign = WebAgentProfile("stranger", "engine", "ui_one",
                                  PROFILE.profile_key, PROFILE.mailbox_key)
        with self.assertRaises(ValueError):
            GuardedBrowserPort(foreign, FaultyBrowser(foreign), guard)
        with self.assertRaises(WebFaultHold):
            guard.require_ready(owner_id="stranger", project_id="engine",
                                agent_id="ui_one")
        with self.assertRaises(WebFaultHold):
            guard.require_bound_port(FaultyBrowser())
        guard.require_bound_port(port)
        self.assertEqual(guard.snapshot().state, "READY")

    def test_port_rejects_foreign_profile_and_missing_credentials(self):
        guard = WebFaultGuard(PROFILE)
        with self.assertRaises(ValueError):
            GuardedBrowserPort(PROFILE, object(), guard)
        with self.assertRaises(ValueError):
            GuardedBrowserPort(PROFILE, FaultyBrowser(
                WebAgentProfile("vlady", "other", "ui_one", "different", "mailbox")),
                guard)
        self.assertEqual(guard.snapshot().state, "READY")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux durable task core only")
class FaultDispatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture = web12_fixtures.CoreHandoffTests(
            "test_approved_task_to_mock_browser_to_strict_v1_response")
        fixture.setUp()
        self.fx = fixture
        self.addCleanup(fixture.doCleanups)
        profile = fixture.sessions.profile(
            owner_id="vlady", project_id="engine", agent_id="ui_one")
        self.guard = WebFaultGuard(profile)
        self.browser = fixture.ports["ui_one"]
        wrapped = GuardedBrowserPort(profile, self.browser, self.guard)
        fixture.sessions._agents["ui_one"].browser = wrapped
        self.path = Path(fixture.fx.temp.name) / "web14-delivery.sqlite"
        self.journal = SQLiteWebDeliveryJournal(self.path)
        self.addCleanup(self.journal.close)
        self.manager = TaskDeliveryManager(fixture.inbox, retry_delay_seconds=0)
        self.dispatcher = WebIdempotentDispatcher(
            fixture.adapter, self.manager, self.journal, fault_guard=self.guard)
        self.task = fixture.current
        self.raw = fixture.request()

    def dispatch(self):
        return self.dispatcher.dispatch(
            self.raw, task_id=self.task, worker_id="ui_one")

    def test_rate_limit_mid_browser_holds_task_lease_and_all_future_dispatch(self):
        def fail(*, model_id, messages):
            self.browser.turns.append(messages)
            raise WebPortFault(WebFaultCode.RATE_LIMITED, retry_after_seconds=120)
        self.browser.complete = fail
        with self.assertRaises(WebDeliveryHold):
            self.dispatch()
        self.assertEqual(self.guard.snapshot().code, WebFaultCode.RATE_LIMITED)
        self.assertEqual(self.guard.snapshot().retry_after_seconds, 120)
        self.assertEqual(self.manager.snapshot(self.task, OWNER).status, "active")
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.RUNNING)
        self.assertIsNotNone(self.fx.inbox._store.capacity_reservation(self.task))
        self.assertEqual(len(self.browser.turns), 1)
        with self.assertRaises(WebDeliveryHold):
            self.dispatch()
        self.assertEqual(len(self.browser.turns), 1)

    def test_existing_hold_blocks_new_delivery_before_creating_ticket(self):
        self.guard._hold(WebFaultCode.SESSION_EXPIRED)
        with self.assertRaises(WebFaultHold):
            self.guard.require_ready(
                owner_id="vlady", project_id="engine", agent_id="ui_one")
        with self.assertRaises(WebDeliveryHold):
            self.dispatch()
        self.assertIsNone(self.manager.snapshot(self.task, OWNER))
        self.assertIsNone(self.journal.snapshot(
            owner_id="vlady", project_id="engine", agent_id="ui_one",
            task_id=self.task))
        self.assertEqual(self.fx.inbox.state(self.task, OWNER), TaskState.ASSIGNED)
        self.assertEqual(len(self.browser.turns), 0)

    def test_detached_guard_does_not_pretend_to_secure_dispatcher(self):
        self.fx.sessions._agents["ui_one"].browser = self.browser
        with self.assertRaises(WebDeliveryHold):
            self.dispatch()
        self.assertIsNone(self.manager.snapshot(self.task, OWNER))
        self.assertEqual(len(self.browser.turns), 0)

    def test_success_path_stays_strict_and_acknowledged(self):
        done = self.dispatch()
        self.assertTrue(done.executed)
        self.assertEqual(done.receipt.state, "ACKED")
        self.assertEqual(done.response.text, "Approved mock answer")
        self.assertEqual(self.guard.snapshot().state, "READY")
        self.assertEqual(len(self.browser.turns), 1)


if __name__ == "__main__":
    unittest.main()
