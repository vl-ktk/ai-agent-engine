"""WEB-05: verified-project-only mock ChatManager with conversation lineage.

In-memory source/offline contract; no real ChatGPT chat creation or durable state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from threading import Lock
from typing import Callable, Protocol

from .chatgpt_web_sessions import (
    WebAgentProfile, WebAgentScopeError, WebAgentSessionRegistry,
)


_CHAT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class CreatedProjectChat:
    """An injected mock port's claim, NOT live ChatGPT UI proof."""

    conversation_id: str
    chatgpt_project_id: str
    profile_key: str
    mailbox_key: str


class ScopedProjectChatPort(Protocol):
    profile_key: str
    mailbox_key: str

    def create_chat(self, *, chatgpt_project_id: str,
                    predecessor_id: str | None) -> CreatedProjectChat: ...


ChatPortFactory = Callable[[WebAgentProfile], ScopedProjectChatPort]


@dataclass(frozen=True, slots=True)
class ProjectChatRecord:
    """Immutable logical record; chat lineage is per agent and project."""

    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    conversation_id: str
    predecessor_id: str | None
    next_id: str | None = None


class ProjectChatManager:
    """One trusted in-process manager; no UI, disk persistence or retries.

    Every create/rotate action obtains a fresh WEB-04 membership proof and
    shares the WEB-03 per-agent FIFO with model turns. No root-chat fallback.
    """

    def __init__(self, sessions: WebAgentSessionRegistry, *,
                 chat_port_factory: ChatPortFactory | None = None) -> None:
        if not isinstance(sessions, WebAgentSessionRegistry):
            raise TypeError("invalid web agent session registry")
        if chat_port_factory is None or not callable(chat_port_factory):
            raise ValueError("trusted project chat port factory required")
        self._sessions = sessions
        self._factory = chat_port_factory
        self._lock = Lock()
        self._ports: dict[str, ScopedProjectChatPort] = {}
        self._port_ids: set[int] = set()
        self._history: dict[str, list[ProjectChatRecord]] = {}
        self._conversation_ids: set[str] = set()

    def _port(self, profile: WebAgentProfile) -> ScopedProjectChatPort:
        with self._lock:
            existing = self._ports.get(profile.agent_id)
        if existing is not None:
            return existing
        try:
            port = self._factory(profile)
        except Exception:
            raise WebAgentScopeError("project chat port initialization failed") from None
        if (port is None or not callable(getattr(port, "create_chat", None))
                or getattr(port, "profile_key", None) != profile.profile_key
                or getattr(port, "mailbox_key", None) != profile.mailbox_key):
            raise WebAgentScopeError("project chat port scope mismatch")
        with self._lock:
            if id(port) in self._port_ids or profile.agent_id in self._ports:
                raise WebAgentScopeError("project chat port is not exclusive")
            self._ports[profile.agent_id] = port
            self._port_ids.add(id(port))
        return port

    def _create(self, *, owner_id: str, project_id: str,
                agent_id: str, initial: bool) -> ProjectChatRecord:
        # Membership and per-agent FIFO are held through the entire port call
        # and local lineage commit. Another agent can proceed independently.
        with self._sessions.scoped_project_operation(
                owner_id=owner_id, project_id=project_id,
                agent_id=agent_id) as (profile, binding):
            with self._lock:
                existing = self._history.get(agent_id, [])
                if initial and existing:
                    raise WebAgentScopeError("initial project chat already exists")
                if not initial and not existing:
                    raise WebAgentScopeError("no active project chat to rotate")
                predecessor = existing[-1].conversation_id if existing else None
            port = self._port(profile)
            try:
                created = port.create_chat(
                    chatgpt_project_id=binding.chatgpt_project_id,
                    predecessor_id=predecessor)
            except Exception:
                # Do not retry: the mock port might already have created it.
                raise WebAgentScopeError("project chat creation unverified") from None
            if (type(created) is not CreatedProjectChat
                    or type(created.conversation_id) is not str
                    or not _CHAT_ID.fullmatch(created.conversation_id)
                    or created.chatgpt_project_id != binding.chatgpt_project_id
                    or created.profile_key != profile.profile_key
                    or created.mailbox_key != profile.mailbox_key):
                raise WebAgentScopeError("project chat creation unverified")
            with self._lock:
                if created.conversation_id in self._conversation_ids:
                    raise WebAgentScopeError("duplicate project conversation")
                lineage = self._history.setdefault(agent_id, [])
                record = ProjectChatRecord(
                    profile.owner_id, profile.project_id, profile.agent_id,
                    binding.chatgpt_project_id, created.conversation_id,
                    predecessor)
                if lineage:
                    lineage[-1] = replace(lineage[-1],
                                          next_id=created.conversation_id)
                lineage.append(record)
                self._conversation_ids.add(created.conversation_id)
                return record

    def create_initial(self, *, owner_id: str, project_id: str,
                       agent_id: str) -> ProjectChatRecord:
        return self._create(owner_id=owner_id, project_id=project_id,
                            agent_id=agent_id, initial=True)

    def rotate(self, *, owner_id: str, project_id: str,
               agent_id: str) -> ProjectChatRecord:
        return self._create(owner_id=owner_id, project_id=project_id,
                            agent_id=agent_id, initial=False)

    def history(self, *, owner_id: str, project_id: str,
                agent_id: str) -> tuple[ProjectChatRecord, ...]:
        self._sessions.profile(owner_id=owner_id, project_id=project_id,
                               agent_id=agent_id)
        with self._lock:
            return tuple(self._history.get(agent_id, ()))

    def active(self, *, owner_id: str, project_id: str,
               agent_id: str) -> ProjectChatRecord:
        records = self.history(owner_id=owner_id, project_id=project_id,
                               agent_id=agent_id)
        if not records:
            raise WebAgentScopeError("no active project chat")
        return records[-1]
