"""Provider-neutral agent loop and single-task role sessions.

No API keys, network calls, persistence or implicit privileges.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from threading import Event, Lock
from typing import Callable, Mapping, Protocol


@dataclass(frozen=True)
class Action:
    name: str
    arguments: Mapping[str, str]


@dataclass(frozen=True)
class ModelTurn:
    """Exactly one of final_text and action must be populated."""

    final_text: str | None = None
    action: Action | None = None

    def __post_init__(self) -> None:
        if (self.final_text is None) == (self.action is None):
            raise ValueError("model turn must contain exactly one outcome")


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Tool:
    name: str
    execute: Callable[[Mapping[str, str]], str]


class Model(Protocol):
    def complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn: ...


class AgentRole(str, Enum):
    """A role describes the model's job; it never grants tool permissions."""

    EXECUTOR = "executor"
    SUPERVISOR = "supervisor"


_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True)
class SessionContext:
    """Owner-provided identity and instructions for exactly one isolated task."""

    project_id: str
    agent_id: str
    role: AgentRole
    instructions: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not _SCOPE_ID.fullmatch(self.project_id):
            raise ValueError("invalid project_id")
        if not isinstance(self.agent_id, str) or not _SCOPE_ID.fullmatch(self.agent_id):
            raise ValueError("invalid agent_id")
        if not isinstance(self.role, AgentRole):
            raise ValueError("invalid agent role")
        if not isinstance(self.instructions, str) or len(self.instructions) > 8_000:
            raise ValueError("invalid agent instructions")

    def system_message(self) -> Message:
        """Trusted metadata, not a substitute for server-side access checks."""
        content = (
            f"Agent role: {self.role.value}\n"
            f"Project: {self.project_id}\n"
            f"Agent: {self.agent_id}"
        )
        if self.instructions:
            content += "\nInstructions: " + self.instructions
        return Message(role="system", content=content)


class StopToken:
    """Thread-safe cooperative cancellation between model/tool steps."""

    def __init__(self) -> None:
        self._event = Event()

    def request_stop(self) -> None:
        self._event.set()

    @property
    def is_stopped(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class AuditEvent:
    step: int
    event: str
    tool: str | None = None


@dataclass(frozen=True)
class AgentResult:
    status: str
    answer: str | None
    steps: int
    audit: tuple[AuditEvent, ...]


class Agent:
    """Calls a model, checks each requested tool against an explicit allowlist and stops on errors.

    This intentionally does NOT run shell commands or obtain credentials. Production
    policy, persistence and provider integrations are subsequent roadmap items.
    """

    def __init__(self, *, model: Model, tools: tuple[Tool, ...], max_steps: int = 5) -> None:
        if max_steps < 1 or max_steps > 20:
            raise ValueError("max_steps must be between 1 and 20")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("tool names must be unique")
        self._model = model
        self._tools = {tool.name: tool for tool in tools}
        self._max_steps = max_steps

    def run(self, prompt: str, *, context: SessionContext | None = None,
            stop: StopToken | None = None) -> AgentResult:
        if not prompt.strip():
            raise ValueError("prompt cannot be empty")
        if context is not None and not isinstance(context, SessionContext):
            raise TypeError("context must be a SessionContext")
        if stop is not None and not isinstance(stop, StopToken):
            raise TypeError("stop must be a StopToken")
        history = [context.system_message()] if context is not None else []
        history.append(Message(role="user", content=prompt))
        audit: list[AuditEvent] = []
        for step in range(1, self._max_steps + 1):
            if stop is not None and stop.is_stopped:
                audit.append(AuditEvent(step, "cancelled"))
                return AgentResult("cancelled", None, step - 1, tuple(audit))
            try:
                turn = self._model.complete(tuple(history), tuple(self._tools))
                if not isinstance(turn, ModelTurn):
                    raise TypeError("invalid model response")
            except (Exception):  # No model exception text or potential secrets enter audit.
                audit.append(AuditEvent(step, "model_error"))
                return AgentResult("failed", None, step, tuple(audit))

            # A stop requested during a blocking model call must prevent the next tool.
            if stop is not None and stop.is_stopped:
                audit.append(AuditEvent(step, "cancelled"))
                return AgentResult("cancelled", None, step, tuple(audit))

            if turn.final_text is not None:
                audit.append(AuditEvent(step, "completed"))
                return AgentResult("done", turn.final_text, step, tuple(audit))

            action = turn.action
            assert action is not None
            if action.name not in self._tools:
                audit.append(AuditEvent(step, "unauthorized_tool", action.name))
                return AgentResult("blocked", None, step, tuple(audit))
            try:
                output = self._tools[action.name].execute(action.arguments)
                if not isinstance(output, str):
                    raise TypeError("tool returned non-string output")
            except (Exception):  # Never reveal input arguments or exception text in audit.
                audit.append(AuditEvent(step, "tool_error", action.name))
                return AgentResult("failed", None, step, tuple(audit))
            audit.append(AuditEvent(step, "tool_ok", action.name))
            history.append(Message(role="assistant", content=f"Called tool: {action.name}"))
            history.append(Message(role="tool", content=output))

        audit.append(AuditEvent(self._max_steps, "step_limit"))
        return AgentResult("blocked", None, self._max_steps, tuple(audit))


class AgentSession:
    """One task and one immutable role context bound to one model adapter.

    A new session and model adapter should be used for every independent task.
    Real access is limited by the explicitly supplied tools, *not* the role.
    """

    def __init__(self, *, context: SessionContext, model: Model,
                 tools: tuple[Tool, ...], max_steps: int = 5) -> None:
        if not isinstance(context, SessionContext):
            raise TypeError("context must be a SessionContext")
        self.context = context
        self._agent = Agent(model=model, tools=tools, max_steps=max_steps)
        self._run_lock = Lock()
        self._started = False

    def run(self, prompt: str, *, stop: StopToken | None = None) -> AgentResult:
        with self._run_lock:
            if self._started:
                raise RuntimeError("agent session already started")
            self._started = True
        return self._agent.run(prompt, context=self.context, stop=stop)


class ScriptedModel:
    """Offline fixture for functional tests; NOT an AI model or an API integration."""

    def __init__(self, turns: tuple[ModelTurn, ...]):
        self._turns = iter(turns)

    def complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn:
        return next(self._turns)
