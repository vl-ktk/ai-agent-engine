"""WEB-02: disabled-by-default, browser-port-only ChatGPT Web model adapter.

There is intentionally no browser implementation, network client, credential
reader, ChatGPT session access, retry or tool execution in this module. The
trusted caller must explicitly supply a browser port (a fake in WEB-02).
Project-scoped sessions and conversation lifecycle belong to WEB-03 onward.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Protocol

from .core import MAX_PROMPT_CHARS, Message, ModelTurn
from .model_routing import ProviderSpec


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
PROVIDER_ID = "chatgpt_web"


class ChatGPTWebUnavailable(RuntimeError):
    """A sanitized refusal; no user text, credentials or browser details."""


@dataclass(frozen=True, slots=True)
class ChatGPTWebConfig:
    """Owner-supplied explicit catalog; no automatic model or provider choice."""

    enabled: bool = False
    models: tuple[str, ...] = ()
    max_reply_chars: int = 20_000

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("invalid web adapter enable switch")
        if (type(self.models) is not tuple
                or any(type(m) is not str or not _MODEL_ID.fullmatch(m)
                       for m in self.models)
                or len(set(self.models)) != len(self.models)):
            raise ValueError("invalid configured web model IDs")
        if self.enabled and not self.models:
            raise ValueError("enabled web adapter requires explicit model IDs")
        if (type(self.max_reply_chars) is not int
                or not 1 <= self.max_reply_chars <= 20_000):
            raise ValueError("invalid web reply limit")


@dataclass(frozen=True, slots=True)
class BrowserTurn:
    """A driver-reported single-turn result (not proof of a real UI model)."""

    model_id: str
    text: str


class BrowserPort(Protocol):
    """Injected synchronous test boundary. No live implementation in WEB-02."""

    def complete(self, *, model_id: str,
                 messages: tuple[Message, ...]) -> BrowserTurn: ...


BrowserFactory = Callable[[str], BrowserPort]


class ChatGPTWebModel:
    """Provider-neutral Model, with exactly one text-only browser turn."""

    def __init__(self, config: ChatGPTWebConfig, model_id: str,
                 browser: BrowserPort | None):
        if not isinstance(config, ChatGPTWebConfig):
            raise TypeError("invalid web adapter config")
        if type(model_id) is not str or model_id not in config.models:
            raise ChatGPTWebUnavailable("unconfigured web model")
        if not config.enabled:
            raise ChatGPTWebUnavailable("chatgpt_web is disabled")
        if browser is None or not callable(getattr(browser, "complete", None)):
            raise ChatGPTWebUnavailable("no trusted web browser port")
        self._config = config
        self._model_id = model_id
        self._browser = browser

    def complete(self, history: tuple[Message, ...],
                 allowed_tools: tuple[str, ...]) -> ModelTurn:
        if allowed_tools:
            raise ChatGPTWebUnavailable("web adapter does not execute tools")
        if (type(history) is not tuple or len(history) not in (1, 2)
                or not all(isinstance(item, Message) for item in history)
                or history[-1].role != "user"
                or (len(history) == 2 and history[0].role != "system")
                or any(type(item.content) is not str or not item.content.strip()
                       or "\x00" in item.content for item in history)
                or sum(len(item.content) for item in history) > MAX_PROMPT_CHARS):
            raise ChatGPTWebUnavailable("invalid web model history")
        # Never retry an unknown outcome: the browser may already have sent it.
        try:
            reply = self._browser.complete(model_id=self._model_id,
                                           messages=history)
        except Exception:
            raise ChatGPTWebUnavailable("web browser operation failed") from None
        if (type(reply) is not BrowserTurn or reply.model_id != self._model_id
                or type(reply.text) is not str or not reply.text.strip()
                or "\x00" in reply.text
                or len(reply.text) > self._config.max_reply_chars):
            raise ChatGPTWebUnavailable("unverified web model response")
        return ModelTurn(final_text=reply.text)


def chatgpt_web_provider_spec(
        config: ChatGPTWebConfig, *,
        browser_factory: BrowserFactory | None = None) -> ProviderSpec:
    """Install via existing ModelRouter; no browser invoked until model use.

    Disabled config remains registerable for honest diagnostics, but refuses
    model construction before calling any browser factory. No browser is
    bundled, and explicitly enabling without a factory also fails closed.
    """
    if not isinstance(config, ChatGPTWebConfig):
        raise TypeError("invalid web adapter config")
    if not config.models:
        raise ValueError("explicit web model catalog required")
    if browser_factory is not None and not callable(browser_factory):
        raise ValueError("invalid trusted browser factory")

    def build(model_id: str) -> ChatGPTWebModel:
        if not config.enabled:
            raise ChatGPTWebUnavailable("chatgpt_web is disabled")
        if browser_factory is None:
            raise ChatGPTWebUnavailable("no trusted web browser factory")
        # ModelRouter already validates model_id against ProviderSpec.models.
        if model_id not in config.models:
            raise ChatGPTWebUnavailable("unconfigured web model")
        try:
            browser = browser_factory(model_id)
        except Exception:
            raise ChatGPTWebUnavailable("web browser initialization failed") from None
        return ChatGPTWebModel(config, model_id, browser)

    return ProviderSpec(provider=PROVIDER_ID, models=config.models,
                        factory=build, supports_tools=False)
