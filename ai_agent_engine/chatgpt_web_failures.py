"""WEB-14: strict mock BrowserPort fault vocabulary and sticky scoped HOLD.

This has no live browser, credential, challenge solver, UI clicker or retry.
Only a trusted injected driver may report a typed observation; untyped
exceptions and malformed responses are treated as unknown external effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
import time
from typing import Callable

from .chatgpt_web import BrowserTurn
from .chatgpt_web_sessions import WebAgentProfile


class WebFaultCode(str, Enum):
    AUTH_REQUIRED = "auth_required"
    SESSION_EXPIRED = "session_expired"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UI_DRIFT = "ui_drift"
    CAPTCHA = "captcha"
    ANTI_BOT = "anti_bot"
    WRONG_PROJECT = "wrong_project"
    WRONG_MODEL = "wrong_model"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN_SEND = "unknown_send"


class WebPortFault(RuntimeError):
    """Typed fault from an injected *trusted* port, never arbitrary page text.

    retry_after_seconds is metadata for a human, never a retry permission.
    """

    def __init__(self, code: WebFaultCode, *,
                 retry_after_seconds: int | None = None):
        if type(code) is not WebFaultCode:
            raise ValueError("closed web fault code required")
        if (retry_after_seconds is not None
                and (code is not WebFaultCode.RATE_LIMITED
                     or type(retry_after_seconds) is not int
                     or not 0 <= retry_after_seconds <= 86_400)):
            raise ValueError("untrusted retry-after metadata")
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__("trusted web port refused")


class WebFaultHold(RuntimeError):
    """Sanitized, sticky human-review requirement; never expose page content."""


@dataclass(frozen=True, slots=True)
class WebFaultSnapshot:
    owner_id: str
    project_id: str
    agent_id: str
    state: str
    code: WebFaultCode | None
    observed_at: float | None
    retry_after_seconds: int | None


class WebFaultGuard:
    """One logical scoped agent; first trusted/unknown failure latches HOLD.

    An in-memory fail-stop latch is intentional. The active WEB-13 task is
    persisted separately; cross-restart agent/session recovery is WEB-15.
    No auto-reset or public bypass after rate limits, auth, or challenges.
    """

    def __init__(self, profile: WebAgentProfile):
        if type(profile) is not WebAgentProfile:
            raise ValueError("trusted web agent profile required")
        self.profile = profile
        self._lock = Lock()
        self._bound_port: object | None = None
        self._code: WebFaultCode | None = None
        self._observed_at: float | None = None
        self._retry_after: int | None = None
        self._hold_sink: Callable[[WebFaultCode], None] | None = None

    def attach_hold_sink(self, sink: Callable[[WebFaultCode], None]) -> None:
        """WEB-15 opt-in trusted durable observer, installed only once."""
        if not callable(sink):
            raise ValueError("trusted fault persistence callback required")
        with self._lock:
            if self._hold_sink is not None or self._code is not None:
                raise WebFaultHold("web fault persistence already bound or held")
            self._hold_sink = sink

    def _scope(self, *, owner_id: str, project_id: str,
               agent_id: str) -> None:
        if (owner_id, project_id, agent_id) != (
                self.profile.owner_id, self.profile.project_id,
                self.profile.agent_id):
            raise WebFaultHold("web agent scope unverified")

    def _claim(self, profile: WebAgentProfile, port: object) -> None:
        if type(profile) is not WebAgentProfile or profile != self.profile:
            raise ValueError("fault port profile mismatch")
        with self._lock:
            if self._bound_port is not None:
                raise ValueError("fault guard already has one browser port")
            self._bound_port = port

    def require_bound_port(self, port: object) -> None:
        with self._lock:
            if port is not self._bound_port or self._bound_port is None:
                raise WebFaultHold("web fault guard is not installed on this browser")

    def require_ready(self, *, owner_id: str, project_id: str,
                      agent_id: str) -> None:
        self._scope(owner_id=owner_id, project_id=project_id, agent_id=agent_id)
        with self._lock:
            if self._code is not None:
                raise WebFaultHold("web agent held pending owner review")

    def _hold(self, code: WebFaultCode,
              retry_after_seconds: int | None = None) -> None:
        if type(code) is not WebFaultCode:
            raise ValueError("closed web fault code required")
        with self._lock:
            if self._code is None:
                if self._hold_sink is not None:
                    try:
                        # Commit first: after restart a recorded fault must
                        # not disappear even if this process exits now.
                        self._hold_sink(code)
                    except Exception:
                        self._code = WebFaultCode.UNKNOWN_SEND
                        self._observed_at = time.time()
                        raise WebFaultHold("web fault persistence unavailable") from None
                self._code = code
                self._retry_after = retry_after_seconds
                self._observed_at = time.time()

    def snapshot(self) -> WebFaultSnapshot:
        with self._lock:
            return WebFaultSnapshot(
                self.profile.owner_id, self.profile.project_id,
                self.profile.agent_id,
                "HOLD" if self._code is not None else "READY",
                self._code, self._observed_at, self._retry_after)


class GuardedBrowserPort:
    """Narrow, no-fallback wrapper around the trusted mock BrowserPort.

    Only `complete` is callable: no login, challenge response, model switch,
    browser navigation, backing-off loop, or untrusted error-string parsing.
    """

    def __init__(self, profile: WebAgentProfile, browser: object,
                 guard: WebFaultGuard, *, max_reply_chars: int = 20_000):
        if (type(profile) is not WebAgentProfile
                or not isinstance(guard, WebFaultGuard)
                or browser is None
                or not callable(getattr(browser, "complete", None))
                or getattr(browser, "profile_key", None) != profile.profile_key
                or getattr(browser, "mailbox_key", None) != profile.mailbox_key
                or type(max_reply_chars) is not int
                or not 1 <= max_reply_chars <= 20_000):
            raise ValueError("trusted scoped browser fault port required")
        self.profile_key = profile.profile_key
        self.mailbox_key = profile.mailbox_key
        self._browser = browser
        self._guard = guard
        self._max_reply_chars = max_reply_chars
        self._turn_lock = Lock()
        guard._claim(profile, self)

    def complete(self, *, model_id: str, messages: tuple) -> BrowserTurn:
        with self._turn_lock:
            self._guard.require_ready(
                owner_id=self._guard.profile.owner_id,
                project_id=self._guard.profile.project_id,
                agent_id=self._guard.profile.agent_id)
            try:
                response = self._browser.complete(
                    model_id=model_id, messages=messages)
            except WebPortFault as fault:
                # A subclass or mutated exception must not smuggle a new
                # category/unsafe delay past the closed vocabulary, leaving
                # the latch READY after an unknown external side effect.
                if (type(fault) is WebPortFault
                        and type(fault.code) is WebFaultCode
                        and (fault.retry_after_seconds is None
                             or (fault.code is WebFaultCode.RATE_LIMITED
                                 and type(fault.retry_after_seconds) is int
                                 and 0 <= fault.retry_after_seconds <= 86_400))):
                    self._guard._hold(fault.code, fault.retry_after_seconds)
                else:
                    self._guard._hold(WebFaultCode.UNKNOWN_SEND)
                raise WebFaultHold("web operation held for owner review") from None
            except Exception:
                self._guard._hold(WebFaultCode.UNKNOWN_SEND)
                raise WebFaultHold("web operation held for owner review") from None
            if type(response) is not BrowserTurn:
                self._guard._hold(WebFaultCode.INVALID_RESPONSE)
                raise WebFaultHold("web response unverified")
            if response.model_id != model_id:
                self._guard._hold(WebFaultCode.WRONG_MODEL)
                raise WebFaultHold("web model selection unverified")
            if (type(response.text) is not str or not response.text.strip()
                    or "\x00" in response.text
                    or len(response.text) > self._max_reply_chars):
                self._guard._hold(WebFaultCode.INVALID_RESPONSE)
                raise WebFaultHold("web response unverified")
            # A trusted controller could have latched a concurrent safety
            # incident while the port was blocked. It must not be bypassed
            # by a syntactically valid reply arriving at the same time.
            self._guard.require_ready(
                owner_id=self._guard.profile.owner_id,
                project_id=self._guard.profile.project_id,
                agent_id=self._guard.profile.agent_id)
            return response
