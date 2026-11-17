"""Explicit provider/model routing for the shared agent runtime.

No provider is queried, authenticated or chosen automatically. Callers supply
trusted adapters and grant tools separately. Real provider integrations are
later stages; this module adds no background work or paid API fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Protocol

from .core import AgentSession, Message, Model, ModelTurn, SessionContext, Tool, ToolRegistry


_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


class ModelFactory(Protocol):
    def __call__(self, model_id: str) -> Model: ...


@dataclass(frozen=True)
class ProviderSpec:
    """Owner-configured provider with explicit available IDs and capabilities."""

    provider: str
    models: tuple[str, ...]
    factory: ModelFactory
    supports_tools: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _PROVIDER_NAME.fullmatch(self.provider):
            raise ValueError("invalid provider identifier")
        if (not isinstance(self.models, tuple) or not self.models
                or any(not isinstance(name, str) or not _MODEL_ID.fullmatch(name)
                       for name in self.models)
                or len(set(self.models)) != len(self.models)):
            raise ValueError("invalid provider model IDs")
        if not callable(self.factory):
            raise ValueError("invalid provider factory")
        if not isinstance(self.supports_tools, bool):
            raise ValueError("invalid provider tool capability")


class ModelRouter:
    """Route a new task to *exactly* the selected provider and model.

    There is deliberately no default provider, fallback, quota retry, dynamic
    discovery or built-in credential reader. The owner must supply a factory.
    """

    def __init__(self, providers: tuple[ProviderSpec, ...]):
        if (not isinstance(providers, tuple)
                or any(not isinstance(p, ProviderSpec) for p in providers)):
            raise ValueError("invalid providers")
        by_name = {item.provider: item for item in providers}
        if len(by_name) != len(providers):
            raise ValueError("duplicate provider")
        self._providers = by_name

    @property
    def available(self) -> tuple[tuple[str, str], ...]:
        """Configured routes only; never invokes an external model catalog."""
        return tuple((item.provider, name)
                     for item in self._providers.values() for name in item.models)

    def _selected(self, provider: str, model_id: str, *, requires_tools: bool) -> ProviderSpec:
        if not isinstance(provider, str) or not isinstance(model_id, str):
            raise ValueError("unrecognized model route")
        item = self._providers.get(provider)
        if item is None or model_id not in item.models:
            raise ValueError("unrecognized model route")
        if requires_tools and not item.supports_tools:
            raise ValueError("selected model route does not support tools")
        return item

    def build_model(self, provider: str, model_id: str, *, requires_tools: bool = False) -> Model:
        """Instantiate only a validated explicit selection; no silent fallback."""
        item = self._selected(provider, model_id, requires_tools=requires_tools)
        model = item.factory(model_id)
        if not callable(getattr(model, "complete", None)):
            raise TypeError("provider factory returned an invalid model adapter")
        return model

    def new_session(self, *, provider: str, model_id: str, context: SessionContext,
                    tools: tuple[Tool, ...] = (), registry: ToolRegistry | None = None,
                    max_steps: int = 5) -> AgentSession:
        """Use the existing context, permission registry and step-bound agent loop."""
        # Fail before instantiating a provider adapter; factories are trusted but
        # may initialize expensive provider clients when real stages are wired.
        if type(max_steps) is not int or not 1 <= max_steps <= 20:
            raise ValueError("max_steps must be between 1 and 20")
        if registry is not None and tools:
            raise ValueError("pass tools or registry, not both")
        if not isinstance(context, SessionContext):
            raise TypeError("context must be a SessionContext")
        if registry is not None and not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be a ToolRegistry")
        if not isinstance(tools, tuple) or any(not isinstance(t, Tool) for t in tools):
            raise TypeError("tools must be Tool definitions")
        has_tools = bool(registry.allowed_names if registry is not None else tools)
        model = self.build_model(provider, model_id, requires_tools=has_tools)
        return AgentSession(context=context, model=model, tools=tools,
                            registry=registry, max_steps=max_steps)


class TextOnlyModel:
    """Normalize a trusted provider's text completion callback to Model.

    An injected callback may represent any text provider, including a future
    owner-triggered Claude Code CLI adapter. This wrapper itself does NOT run
    a CLI, access subscription tokens or provide tool support.
    """

    def __init__(self, complete_text: Callable[[tuple[Message, ...]], str], *,
                 max_reply_chars: int = 20_000):
        if not callable(complete_text):
            raise ValueError("invalid text model callback")
        if not isinstance(max_reply_chars, int) or not 1 <= max_reply_chars <= 1_048_576:
            raise ValueError("invalid text reply limit")
        self._complete_text = complete_text
        self._max_reply_chars = max_reply_chars

    def complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn:
        if allowed_tools:
            raise ValueError("text-only adapter cannot call tools")
        if not (isinstance(history, tuple) and len(history) in (1, 2)
                and all(isinstance(message, Message) for message in history)
                and history[-1].role == "user"
                and (len(history) == 1 or history[0].role == "system")):
            raise ValueError("invalid text-only model history")
        result = self._complete_text(history)
        if (not isinstance(result, str) or not result.strip()
                or len(result) > self._max_reply_chars):
            raise ValueError("invalid text model response")
        return ModelTurn(final_text=result)
