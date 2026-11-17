"""Provider-neutral agent loop and single-task role sessions.

No API keys, network calls, persistence or implicit privileges.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
from threading import Event, Lock
from typing import Callable, Mapping, Protocol

MAX_PROMPT_CHARS = 65_536
MAX_FINAL_TEXT_CHARS = 1_048_576


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
        if self.final_text is not None and (
            not isinstance(self.final_text, str)
            or not self.final_text.strip()
            or len(self.final_text) > MAX_FINAL_TEXT_CHARS
        ):
            raise ValueError("invalid model final text")


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Tool:
    name: str
    execute: Callable[[Mapping[str, str]], str]
    required_args: tuple[str, ...] = ()
    optional_args: tuple[str, ...] = ()
    max_argument_bytes: int = 16_384
    max_output_chars: int = 20_000

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name):
            raise ValueError("invalid tool name")
        if not callable(self.execute):
            raise ValueError("invalid tool executor")
        if not isinstance(self.required_args, tuple) or not isinstance(self.optional_args, tuple):
            raise ValueError("invalid tool argument schema")
        names = self.required_args + self.optional_args
        if (any(not isinstance(key, str) for key in names)
                or len(set(names)) != len(names)
                or any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                       for key in names)):
            raise ValueError("invalid tool argument schema")
        if not 1 <= self.max_argument_bytes <= 1_048_576:
            raise ValueError("invalid tool argument limit")
        if not 1 <= self.max_output_chars <= 1_048_576:
            raise ValueError("invalid tool output limit")

    def checked_arguments(self, arguments: object) -> dict[str, str]:
        """Copy only exact, bounded UTF-8 string arguments; reject unknown keys."""
        if not isinstance(arguments, Mapping):
            raise ValueError("invalid tool arguments")
        if (not set(self.required_args).issubset(arguments)
                or not set(arguments).issubset(self.required_args + self.optional_args)):
            raise ValueError("invalid tool arguments")
        size = 0
        checked: dict[str, str] = {}
        for key, value in arguments.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("invalid tool arguments")
            try:
                size += len(key.encode("utf-8")) + len(value.encode("utf-8"))
            except UnicodeError:
                raise ValueError("invalid tool arguments") from None
            if size > self.max_argument_bytes:
                raise ValueError("invalid tool arguments")
            checked[key] = value
        return checked


class ToolRegistry:
    """Trusted tool definitions with an explicit per-session permission subset."""

    def __init__(self, tools: tuple[Tool, ...], *, allowed: tuple[str, ...] | None = None):
        if not isinstance(tools, tuple) or any(not isinstance(tool, Tool) for tool in tools):
            raise ValueError("invalid tool definitions")
        definitions = {tool.name: tool for tool in tools}
        if len(definitions) != len(tools):
            raise ValueError("tool names must be unique")
        grants = tuple(definitions) if allowed is None else allowed
        if (not isinstance(grants, tuple) or any(not isinstance(name, str) for name in grants)
                or len(set(grants)) != len(grants)
                or any(name not in definitions for name in grants)):
            raise ValueError("invalid tool grants")
        self._definitions = definitions
        self._grants = grants

    @property
    def allowed_names(self) -> tuple[str, ...]:
        return self._grants

    def granted_tool(self, name: object) -> Tool | None:
        if not isinstance(name, str) or name not in self._grants:
            return None
        return self._definitions[name]


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
    project_id: str | None = None
    agent_id: str | None = None
    timestamp_utc: str = ""


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

    def __init__(self, *, model: Model, tools: tuple[Tool, ...] = (),
                 registry: ToolRegistry | None = None, max_steps: int = 5) -> None:
        if max_steps < 1 or max_steps > 20:
            raise ValueError("max_steps must be between 1 and 20")
        if registry is not None and tools:
            raise ValueError("pass tools or registry, not both")
        self._model = model
        self._registry = registry if registry is not None else ToolRegistry(tools)
        self._max_steps = max_steps

    def run(self, prompt: str, *, context: SessionContext | None = None,
            stop: StopToken | None = None) -> AgentResult:
        if (not isinstance(prompt, str) or not prompt.strip()
                or len(prompt) > MAX_PROMPT_CHARS):
            raise ValueError("prompt must be a nonblank bounded string")
        if context is not None and not isinstance(context, SessionContext):
            raise TypeError("context must be a SessionContext")
        if stop is not None and not isinstance(stop, StopToken):
            raise TypeError("stop must be a StopToken")
        history = [context.system_message()] if context is not None else []
        history.append(Message(role="user", content=prompt))
        audit: list[AuditEvent] = []
        def record(step: int, event: str, tool: str | None = None) -> None:
            audit.append(AuditEvent(
                step, event, tool,
                context.project_id if context is not None else None,
                context.agent_id if context is not None else None,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))

        for step in range(1, self._max_steps + 1):
            if stop is not None and stop.is_stopped:
                record(step, "cancelled")
                return AgentResult("cancelled", None, step - 1, tuple(audit))
            try:
                turn = self._model.complete(tuple(history), self._registry.allowed_names)
                if not isinstance(turn, ModelTurn):
                    raise TypeError("invalid model response")
            except (Exception):  # No model exception text or potential secrets enter audit.
                record(step, "model_error")
                return AgentResult("failed", None, step, tuple(audit))

            # A stop requested during a blocking model call must prevent the next tool.
            if stop is not None and stop.is_stopped:
                record(step, "cancelled")
                return AgentResult("cancelled", None, step, tuple(audit))

            if turn.final_text is not None:
                record(step, "completed")
                return AgentResult("done", turn.final_text, step, tuple(audit))

            action = turn.action
            if not isinstance(action, Action):
                record(step, "invalid_model_action")
                return AgentResult("blocked", None, step, tuple(audit))
            tool = self._registry.granted_tool(action.name)
            if tool is None:
                # The untrusted tool name might itself contain private text.
                record(step, "unauthorized_tool")
                return AgentResult("blocked", None, step, tuple(audit))
            try:
                checked_args = tool.checked_arguments(action.arguments)
            except (Exception):
                record(step, "invalid_tool_arguments", tool.name)
                return AgentResult("blocked", None, step, tuple(audit))
            try:
                output = tool.execute(checked_args)
                if not isinstance(output, str):
                    raise TypeError("tool returned non-string output")
                if len(output) > tool.max_output_chars:
                    record(step, "tool_output_limit", tool.name)
                    return AgentResult("failed", None, step, tuple(audit))
            except (Exception):  # Never reveal input arguments or exception text in audit.
                record(step, "tool_error", tool.name)
                return AgentResult("failed", None, step, tuple(audit))
            record(step, "tool_ok", tool.name)
            history.append(Message(role="assistant", content=f"Called tool: {tool.name}"))
            history.append(Message(role="tool", content=output))

        record(self._max_steps, "step_limit")
        return AgentResult("blocked", None, self._max_steps, tuple(audit))


class AgentSession:
    """One task and one immutable role context bound to one model adapter.

    A new session and model adapter should be used for every independent task.
    Real access is limited by the explicitly supplied tools, *not* the role.
    """

    def __init__(self, *, context: SessionContext, model: Model,
                 tools: tuple[Tool, ...] = (), registry: ToolRegistry | None = None,
                 max_steps: int = 5) -> None:
        if not isinstance(context, SessionContext):
            raise TypeError("context must be a SessionContext")
        self.context = context
        self._agent = Agent(model=model, tools=tools, registry=registry, max_steps=max_steps)
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
