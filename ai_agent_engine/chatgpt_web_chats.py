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
    task_id: str | None = None
    rotation_id: str | None = None


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
                agent_id: str, initial: bool,
                expected_predecessor: str | None = None,
                task_id: str | None = None,
                rotation_id: str | None = None) -> ProjectChatRecord:
        # Membership and per-agent FIFO are held through the entire port call
        # and local lineage commit. Another agent can proceed independently.
        with self._sessions.scoped_project_operation(
                owner_id=owner_id, project_id=project_id,
                agent_id=agent_id) as (profile, binding):
            with self._lock:
                existing = self._history.get(agent_id, [])
                if rotation_id is not None:
                    replay = next((record for record in existing
                                   if record.rotation_id == rotation_id), None)
                    if replay is not None:
                        if (replay.task_id != task_id
                                or replay.predecessor_id != expected_predecessor):
                            raise WebAgentScopeError("rotation idempotency collision")
                        return replay
                if initial and existing:
                    raise WebAgentScopeError("initial project chat already exists")
                if not initial and not existing:
                    raise WebAgentScopeError("no active project chat to rotate")
                predecessor = existing[-1].conversation_id if existing else None
                if (rotation_id is not None
                        and predecessor != expected_predecessor):
                    raise WebAgentScopeError("rotation predecessor changed")
            port = self._port(profile)
            try:
                if rotation_id is not None:
                    create_once = getattr(port, "create_or_get_chat", None)
                    if not callable(create_once):
                        raise WebAgentScopeError("idempotent project chat port unavailable")
                    created = create_once(
                        chatgpt_project_id=binding.chatgpt_project_id,
                        predecessor_id=predecessor, task_id=task_id,
                        rotation_id=rotation_id)
                else:
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
                    predecessor, task_id=task_id, rotation_id=rotation_id)
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

    def rotate_idempotent(self, *, owner_id: str, project_id: str,
                          agent_id: str, expected_predecessor: str,
                          task_id: str, rotation_id: str) -> ProjectChatRecord:
        """WEB-10 mock: same operation can be reconciled after uncertainty.

        Port MUST implement `create_or_get_chat` with server-side idempotency
        semantics. No fallback to non-idempotent WEB-05 `create_chat`.
        """
        if (any(type(x) is not str or not _CHAT_ID.fullmatch(x)
                for x in (expected_predecessor, task_id, rotation_id))):
            raise WebAgentScopeError("invalid rotation operation identity")
        return self._create(owner_id=owner_id, project_id=project_id,
                            agent_id=agent_id, initial=False,
                            expected_predecessor=expected_predecessor,
                            task_id=task_id, rotation_id=rotation_id)

    def restore_verified(self, *, owner_id: str, project_id: str,
                         agent_id: str,
                         records: tuple[ProjectChatRecord, ...]) -> None:
        """Restore an entire trusted mock chain after a controller restart.

        The caller is responsible for authenticating the journal. This does
        not recreate browser tabs or prove live ChatGPT Project membership.
        """
        if type(records) is not tuple or not records:
            raise WebAgentScopeError("missing verified chat history")
        with self._sessions.scoped_project_operation(
                owner_id=owner_id, project_id=project_id,
                agent_id=agent_id) as (profile, binding):
            seen: set[str] = set()
            rotations: set[str] = set()
            for i, record in enumerate(records):
                if (type(record) is not ProjectChatRecord
                        or (record.owner_id, record.project_id, record.agent_id)
                        != (owner_id, project_id, agent_id)
                        or record.chatgpt_project_id != binding.chatgpt_project_id
                        or type(record.conversation_id) is not str
                        or not _CHAT_ID.fullmatch(record.conversation_id)
                        or record.conversation_id in seen
                        or (record.rotation_id is not None
                            and record.rotation_id in rotations)
                        or record.predecessor_id != (
                            records[i - 1].conversation_id if i else None)
                        or record.next_id != (
                            records[i + 1].conversation_id
                            if i + 1 < len(records) else None)
                        or (i == 0 and (record.task_id is not None
                                         or record.rotation_id is not None))
                        or (i > 0 and ((record.task_id is None)
                                       != (record.rotation_id is None)))
                        or any(x is not None and (type(x) is not str
                               or not _CHAT_ID.fullmatch(x))
                               for x in (record.task_id, record.rotation_id))):
                    raise WebAgentScopeError("invalid restored chat lineage")
                seen.add(record.conversation_id)
                if record.rotation_id is not None:
                    rotations.add(record.rotation_id)
            with self._lock:
                if self._history.get(agent_id) or seen & self._conversation_ids:
                    raise WebAgentScopeError("chat history restore collision")
                self._history[agent_id] = list(records)
                self._conversation_ids.update(seen)

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
