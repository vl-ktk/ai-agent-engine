"""WEB-03: owner-scoped in-memory agent identities and mock browser isolation.

WEB-04 adds an opt-in trusted mock Project membership gate. No real ChatGPT
Project membership, persistent mailbox, disk browser profile or live web
access is implied here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import re
from threading import Condition, Lock
from typing import TYPE_CHECKING, Callable, Iterator, Protocol

from .chatgpt_web import (
    BrowserPort, ChatGPTWebConfig, ChatGPTWebModel, ChatGPTWebUnavailable,
)
from .core import AgentResult, AgentRole, AgentSession, SessionContext

if TYPE_CHECKING:
    from .chatgpt_web_projects import (
        ProjectMembershipVerifier, TrustedChatGPTProjectBinding,
        TrustedChatGPTProjectRegistry,
    )


_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ACTION_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


class WebAgentScopeError(PermissionError):
    """Sanitized denial: unknown or mismatched trusted identity/scope."""


@dataclass(frozen=True, slots=True)
class WebAgentIdentity:
    """Trusted stable owner/project/agent binding, not an authorization grant."""

    owner_id: str
    project_id: str
    agent_id: str
    role: AgentRole
    instructions: str = ""
    declared_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(type(value) is not str or not _SCOPE_ID.fullmatch(value)
               for value in (self.owner_id, self.project_id, self.agent_id)):
            raise ValueError("invalid web agent identity")
        if not isinstance(self.role, AgentRole):
            raise ValueError("invalid web agent role")
        if type(self.instructions) is not str or len(self.instructions) > 8_000:
            raise ValueError("invalid web agent instructions")
        if (type(self.declared_actions) is not tuple
                or any(type(action) is not str or not _ACTION_NAME.fullmatch(action)
                       for action in self.declared_actions)
                or len(set(self.declared_actions)) != len(self.declared_actions)):
            raise ValueError("invalid declared agent actions")


@dataclass(frozen=True, slots=True)
class WebAgentProfile:
    """Opaque logical namespaces; not actual browser dirs or ChatGPT Projects."""

    owner_id: str
    project_id: str
    agent_id: str
    profile_key: str
    mailbox_key: str


def _profile_for(identity: WebAgentIdentity) -> WebAgentProfile:
    # Length-prefixed source values avoid tuple-encoding ambiguity. Domain
    # separation prevents accidentally equating profile and mailbox handles.
    pieces = (identity.owner_id, identity.project_id, identity.agent_id)
    payload = b"".join(len(x.encode("utf-8")).to_bytes(2, "big") + x.encode("utf-8")
                       for x in pieces)

    def handle(domain: bytes) -> str:
        return hashlib.sha256(domain + b"\0" + payload).hexdigest()

    return WebAgentProfile(identity.owner_id, identity.project_id,
                           identity.agent_id, "profile-" + handle(b"web-agent-v1"),
                           "mailbox-" + handle(b"web-mailbox-v1"))


class _AgentSerialQueue:
    """Per-agent FIFO turn admission, with no cross-agent lock during work."""

    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._next_ticket = 0
        self._serving = 0

    @property
    def pending(self) -> int:
        with self._condition:
            return self._next_ticket - self._serving

    @contextmanager
    def turn(self) -> Iterator[None]:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            while ticket != self._serving:
                self._condition.wait()
        try:
            yield
        finally:
            with self._condition:
                self._serving += 1
                self._condition.notify_all()


class ScopedBrowserPort(BrowserPort, Protocol):
    """A mock port must attest to the exact logical profile it was given."""

    profile_key: str
    mailbox_key: str


@dataclass(slots=True)
class _AgentSlot:
    identity: WebAgentIdentity
    profile: WebAgentProfile
    browser: ScopedBrowserPort
    queue: _AgentSerialQueue


ProfileBrowserFactory = Callable[[WebAgentProfile], ScopedBrowserPort]


class WebAgentSessionRegistry:
    """Explicit stable agent identity → exclusive mock port and private queue.

    Only trusted server code may register identities or supply scope IDs.
    Matching IDs here prevent accidental cross-agent routing; they are NOT
    login credentials, a real browser profile sandbox or TaskPermissionGate.
    """

    def __init__(self, config: ChatGPTWebConfig, *,
                 browser_factory: ProfileBrowserFactory | None = None,
                 project_registry: TrustedChatGPTProjectRegistry | None = None,
                 project_verifier: ProjectMembershipVerifier | None = None) -> None:
        if not isinstance(config, ChatGPTWebConfig):
            raise TypeError("invalid web adapter config")
        if browser_factory is not None and not callable(browser_factory):
            raise ValueError("invalid scoped browser factory")
        if (project_registry is None) != (project_verifier is None):
            raise ValueError("project registry and verifier must be paired")
        if project_registry is not None:
            from .chatgpt_web_projects import TrustedChatGPTProjectRegistry
            if (not isinstance(project_registry, TrustedChatGPTProjectRegistry)
                    or not callable(getattr(project_verifier, "verify", None))):
                raise ValueError("invalid trusted project gate")
        self._config = config
        self._browser_factory = browser_factory
        self._project_registry = project_registry
        self._project_verifier = project_verifier
        self._lock = Lock()
        self._agents: dict[str, _AgentSlot] = {}
        self._browser_ids: set[int] = set()

    def register(self, identity: WebAgentIdentity) -> WebAgentProfile:
        if not isinstance(identity, WebAgentIdentity):
            raise TypeError("invalid agent identity")
        if not self._config.enabled:
            raise ChatGPTWebUnavailable("chatgpt_web is disabled")
        if self._browser_factory is None:
            raise ChatGPTWebUnavailable("no trusted scoped browser factory")
        if self._project_registry is not None:
            self._project_registry.binding_for(owner_id=identity.owner_id,
                                               project_id=identity.project_id)
        with self._lock:
            # Even the same agent must not be rebound silently. A browser
            # session and its scope are pinned for the registry lifetime.
            if identity.agent_id in self._agents:
                raise WebAgentScopeError("agent identity already registered")
            profile = _profile_for(identity)
            try:
                browser = self._browser_factory(profile)
            except Exception:
                raise ChatGPTWebUnavailable("scoped browser initialization failed") from None
            if (browser is None or not callable(getattr(browser, "complete", None))
                    or getattr(browser, "profile_key", None) != profile.profile_key
                    or getattr(browser, "mailbox_key", None) != profile.mailbox_key
                    or id(browser) in self._browser_ids):
                raise ChatGPTWebUnavailable("browser port scope is not exclusive")
            self._agents[identity.agent_id] = _AgentSlot(
                identity, profile, browser, _AgentSerialQueue())
            self._browser_ids.add(id(browser))
            return profile

    def _slot(self, *, owner_id: str, project_id: str,
              agent_id: str) -> _AgentSlot:
        if any(type(x) is not str or not _SCOPE_ID.fullmatch(x)
               for x in (owner_id, project_id, agent_id)):
            raise WebAgentScopeError("invalid web agent scope")
        with self._lock:
            slot = self._agents.get(agent_id)
            if (slot is None or slot.identity.owner_id != owner_id
                    or slot.identity.project_id != project_id):
                raise WebAgentScopeError("web agent scope mismatch")
            return slot

    def profile(self, *, owner_id: str, project_id: str,
                agent_id: str) -> WebAgentProfile:
        return self._slot(owner_id=owner_id, project_id=project_id,
                          agent_id=agent_id).profile

    def identity(self, *, owner_id: str, project_id: str,
                 agent_id: str) -> WebAgentIdentity:
        """Only declared metadata; action names do not grant tool access."""
        return self._slot(owner_id=owner_id, project_id=project_id,
                          agent_id=agent_id).identity

    def pending(self, *, owner_id: str, project_id: str,
                agent_id: str) -> int:
        return self._slot(owner_id=owner_id, project_id=project_id,
                          agent_id=agent_id).queue.pending

    def project_binding(self, *, owner_id: str, project_id: str,
                        agent_id: str) -> TrustedChatGPTProjectBinding:
        """Only trusted registered identities may inspect their mapping."""
        slot = self._slot(owner_id=owner_id, project_id=project_id,
                          agent_id=agent_id)
        if self._project_registry is None:
            raise WebAgentScopeError("no trusted ChatGPT Project gate")
        return self._project_registry.binding_for(
            owner_id=slot.identity.owner_id, project_id=slot.identity.project_id)

    @contextmanager
    def scoped_project_operation(self, *, owner_id: str, project_id: str,
                                 agent_id: str) -> Iterator[
                                     tuple[WebAgentProfile, TrustedChatGPTProjectBinding]]:
        """WEB-05: verify membership and hold the agent FIFO for chat actions.

        The injected chat port must not act before this context is entered.
        This contract does not attest real ChatGPT UI state.
        """
        slot = self._slot(owner_id=owner_id, project_id=project_id,
                          agent_id=agent_id)
        if self._project_registry is None:
            raise WebAgentScopeError("no trusted ChatGPT Project gate")
        with slot.queue.turn():
            self._project_registry.verify_member(
                profile=slot.profile, verifier=self._project_verifier)
            binding = self._project_registry.binding_for(
                owner_id=slot.identity.owner_id,
                project_id=slot.identity.project_id)
            yield slot.profile, binding

    def run(self, *, owner_id: str, project_id: str,
            agent_id: str, model_id: str, prompt: str) -> AgentResult:
        """One scoped synthetic task, FIFO per agent, parallel across agents.

        Every turn uses fresh AgentSession and trusted pinned instructions,
        while the agent-owned browser port and logical profile stay stable.
        This is not a persistent chat or an external side-effect lease.
        """
        slot = self._slot(owner_id=owner_id, project_id=project_id,
                          agent_id=agent_id)
        with slot.queue.turn():
            if self._project_registry is not None:
                self._project_registry.verify_member(
                    profile=slot.profile, verifier=self._project_verifier)
            model = ChatGPTWebModel(self._config, model_id, slot.browser)
            context = SessionContext(slot.identity.project_id,
                                     slot.identity.agent_id,
                                     slot.identity.role, slot.identity.instructions)
            return AgentSession(context=context, model=model).run(prompt)
