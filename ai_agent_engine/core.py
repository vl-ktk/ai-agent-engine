"""Minimal model/tool loop. No API keys, network calls or implicit privileges."""

from __future__ import annotations

from dataclasses import dataclass
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

    def run(self, prompt: str) -> AgentResult:
        if not prompt.strip():
            raise ValueError("prompt cannot be empty")
        history = [Message(role="user", content=prompt)]
        audit: list[AuditEvent] = []
        for step in range(1, self._max_steps + 1):
            try:
                turn = self._model.complete(tuple(history), tuple(self._tools))
                if not isinstance(turn, ModelTurn):
                    raise TypeError("invalid model response")
            except (Exception):  # No model exception text or potential secrets enter audit.
                audit.append(AuditEvent(step, "model_error"))
                return AgentResult("failed", None, step, tuple(audit))

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


class ScriptedModel:
    """Offline fixture for functional tests; NOT an AI model or an API integration."""

    def __init__(self, turns: tuple[ModelTurn, ...]):
        self._turns = iter(turns)

    def complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn:
        return next(self._turns)
