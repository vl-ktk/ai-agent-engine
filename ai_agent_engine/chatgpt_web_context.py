"""WEB-09: conservative, scoped context pressure preflight for mock chats.

No real token counter or browser access is included. In particular, a bounded
estimate is only as trustworthy as its injected, independently verified port.
Actual chat rotation and durable checkpoints are WEB-10/11 responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from threading import Lock
from typing import Protocol

from .chatgpt_web_chats import ProjectChatManager
from .chatgpt_web_projects import TrustedChatGPTProjectBinding
from .chatgpt_web_sessions import (
    WebAgentProfile, WebAgentScopeError, WebAgentSessionRegistry,
)


_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_KINDS = ("exact", "bounded", "unknown")


class WebContextPressureError(WebAgentScopeError):
    """Sanitized HOLD/rotation refusal, never containing private prompts."""


@dataclass(frozen=True, slots=True)
class ContextThresholds:
    warn_percent: int = 60
    rotate_percent: int = 75
    emergency_percent: int = 90

    def __post_init__(self) -> None:
        if (any(type(x) is not int for x in (
                self.warn_percent, self.rotate_percent, self.emergency_percent))
                or not 0 < self.warn_percent < self.rotate_percent
                < self.emergency_percent <= 100):
            raise ValueError("invalid ordered context thresholds")


@dataclass(frozen=True, slots=True)
class TrustedAgentContextPolicy:
    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    context_window_tokens: int
    thresholds: ContextThresholds = ContextThresholds()
    response_reserve_tokens: int = 256

    def __post_init__(self) -> None:
        if (any(type(x) is not str or not _SCOPE_ID.fullmatch(x)
                for x in (self.owner_id, self.project_id, self.agent_id,
                          self.chatgpt_project_id))
                or type(self.context_window_tokens) is not int
                or not 1 <= self.context_window_tokens <= 100_000_000
                or type(self.thresholds) is not ContextThresholds
                or type(self.response_reserve_tokens) is not int
                or not 0 <= self.response_reserve_tokens
                < self.context_window_tokens):
            raise ValueError("invalid trusted context budget policy")


@dataclass(frozen=True, slots=True)
class ContextUsageEvidence:
    """Claim from a mock port; bounded means a genuine *upper* estimate.

    A point estimate without a defensible upper limit is `unknown`, not a
    low-usage signal. Tokens describe the current chat prior to this turn.
    """

    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    profile_key: str
    mailbox_key: str
    conversation_id: str
    kind: str
    used_tokens: int | None
    upper_bound_tokens: int | None
    current: bool
    verified: bool


class ScopedContextUsageSource(Protocol):
    def read_current(self, *, binding: TrustedChatGPTProjectBinding,
                     profile: WebAgentProfile,
                     conversation_id: str) -> ContextUsageEvidence: ...


@dataclass(frozen=True, slots=True)
class ContextPressureDecision:
    owner_id: str
    project_id: str
    agent_id: str
    conversation_id: str
    action: str  # OK, WARN, ROTATE, EMERGENCY, HOLD
    projected_upper_tokens: int | None
    context_window_tokens: int
    estimate_kind: str


class WebContextMonitor:
    """One pinned policy per agent, evaluated inside WEB-03's agent FIFO.

    At 60% a warning is recorded and a turn may proceed; at 75% the caller
    must rotate (WEB-10), and at 90%/unknown it must HOLD. Neither this class
    nor a rejected turn creates a chat or retries browser operations.
    """

    def __init__(self, sessions: WebAgentSessionRegistry,
                 chats: ProjectChatManager, *,
                 usage_source: ScopedContextUsageSource):
        if (not isinstance(sessions, WebAgentSessionRegistry)
                or not isinstance(chats, ProjectChatManager)
                or chats._sessions is not sessions
                or not callable(getattr(usage_source, "read_current", None))):
            raise ValueError("trusted scoped context monitor dependencies required")
        self.sessions = sessions
        self._chats = chats
        self._source = usage_source
        self._lock = Lock()
        self._policies: dict[str, TrustedAgentContextPolicy] = {}
        self._last: dict[str, ContextPressureDecision] = {}
        self._seen: dict[str, tuple[str, int]] = {}

    def register(self, policy: TrustedAgentContextPolicy) -> None:
        if type(policy) is not TrustedAgentContextPolicy:
            raise TypeError("invalid trusted context policy")
        binding = self.sessions.project_binding(
            owner_id=policy.owner_id, project_id=policy.project_id,
            agent_id=policy.agent_id)
        if binding.chatgpt_project_id != policy.chatgpt_project_id:
            raise WebContextPressureError("context policy project mismatch")
        with self._lock:
            if policy.agent_id in self._policies:
                raise WebContextPressureError("context policy already registered")
            self._policies[policy.agent_id] = policy

    def last_decision(self, *, owner_id: str, project_id: str,
                      agent_id: str) -> ContextPressureDecision | None:
        self.sessions.profile(owner_id=owner_id, project_id=project_id,
                              agent_id=agent_id)
        with self._lock:
            return self._last.get(agent_id)

    def preflight(self, *, binding: TrustedChatGPTProjectBinding,
                  profile: WebAgentProfile, prompt: str,
                  system_message: str) -> ContextPressureDecision:
        """Run while holding the agent FIFO, before BrowserPort.complete."""
        with self._lock:
            policy = self._policies.get(profile.agent_id)
        if (policy is None or policy.owner_id != profile.owner_id
                or policy.project_id != profile.project_id
                or policy.chatgpt_project_id != binding.chatgpt_project_id):
            raise WebContextPressureError("trusted context policy unavailable")
        with self._lock:
            self._last.pop(profile.agent_id, None)
        try:
            chat = self._chats.active(owner_id=profile.owner_id,
                                      project_id=profile.project_id,
                                      agent_id=profile.agent_id)
        except WebAgentScopeError:
            raise WebContextPressureError("active context chat unavailable") from None
        if chat.chatgpt_project_id != binding.chatgpt_project_id:
            raise WebContextPressureError("active chat project mismatch")
        try:
            evidence = self._source.read_current(
                binding=binding, profile=profile,
                conversation_id=chat.conversation_id)
        except Exception:
            raise WebContextPressureError("context observation unavailable") from None

        if (type(evidence) is not ContextUsageEvidence
                or evidence.owner_id != profile.owner_id
                or evidence.project_id != profile.project_id
                or evidence.agent_id != profile.agent_id
                or evidence.chatgpt_project_id != binding.chatgpt_project_id
                or evidence.profile_key != profile.profile_key
                or evidence.mailbox_key != profile.mailbox_key
                or evidence.conversation_id != chat.conversation_id
                or evidence.current is not True or evidence.verified is not True
                or type(evidence.kind) is not str or evidence.kind not in _KINDS):
            raise WebContextPressureError("unverified scoped context observation")

        kind = evidence.kind
        upper: int | None = None
        if kind == "unknown":
            if evidence.used_tokens is not None or evidence.upper_bound_tokens is not None:
                raise WebContextPressureError("malformed unknown context observation")
            action = "HOLD"
        else:
            lower = evidence.used_tokens
            bound = evidence.upper_bound_tokens
            if (type(lower) is not int or type(bound) is not int
                    or lower < 0 or bound < lower
                    or (kind == "exact" and lower != bound)
                    or bound > 100_000_000):
                raise WebContextPressureError("invalid context estimate bounds")
            with self._lock:
                previous = self._seen.get(profile.agent_id)
                if (previous is not None and previous[0] == chat.conversation_id
                        and bound < previous[1]):
                    raise WebContextPressureError("context estimate regressed")
                # Retain a floor; a later bounded estimate cannot invalidate
                # an earlier trusted lower bound even if its point value falls.
                self._seen[profile.agent_id] = (
                    chat.conversation_id,
                    max(lower, previous[1]) if previous is not None
                    and previous[0] == chat.conversation_id else lower)
            # UTF-8 bytes are a conservative proxy for input token count,
            # plus explicit response reserve. Instruction text is counted
            # again because the observation cannot prove it is already cached.
            if (type(prompt) is not str or type(system_message) is not str):
                raise WebContextPressureError("invalid context preflight input")
            upper = (bound + len(prompt.encode("utf-8"))
                     + len(system_message.encode("utf-8"))
                     + policy.response_reserve_tokens + 32)
            thresholds = policy.thresholds
            if upper * 100 >= policy.context_window_tokens * thresholds.emergency_percent:
                action = "EMERGENCY"
            elif upper * 100 >= policy.context_window_tokens * thresholds.rotate_percent:
                action = "ROTATE"
            elif upper * 100 >= policy.context_window_tokens * thresholds.warn_percent:
                action = "WARN"
            else:
                action = "OK"

        decision = ContextPressureDecision(
            profile.owner_id, profile.project_id, profile.agent_id,
            chat.conversation_id, action, upper,
            policy.context_window_tokens, kind)
        with self._lock:
            self._last[profile.agent_id] = decision
        if action in ("HOLD", "EMERGENCY", "ROTATE"):
            raise WebContextPressureError("context " + action.lower()
                                          + " requires safe handoff")
        return decision
