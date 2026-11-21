"""WEB-07: owner-configured reasoning choices and mock-only effort evidence.

These are trusted test fixtures, not a real ChatGPT UI or entitlement probe.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from .chatgpt_web_projects import TrustedChatGPTProjectBinding
from .chatgpt_web_sessions import WebAgentProfile, WebAgentScopeError


_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_EFFORT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")


class WebReasoningSelectionError(WebAgentScopeError):
    """Sanitized fail-closed reasoning-effort refusal."""


@dataclass(frozen=True, slots=True)
class TrustedModelReasoningChoices:
    """Configured mock effort IDs for exactly one model, not UI capabilities."""

    model_id: str
    reasoning_efforts: tuple[str, ...]

    def __post_init__(self) -> None:
        if (type(self.model_id) is not str or not _MODEL_ID.fullmatch(self.model_id)
                or type(self.reasoning_efforts) is not tuple
                or not self.reasoning_efforts
                or any(type(effort) is not str or not _EFFORT_ID.fullmatch(effort)
                       for effort in self.reasoning_efforts)
                or len(set(self.reasoning_efforts)) != len(self.reasoning_efforts)):
            raise ValueError("invalid trusted model reasoning choices")


@dataclass(frozen=True, slots=True)
class TrustedProjectReasoningCatalog:
    chatgpt_project_id: str
    model_choices: tuple[TrustedModelReasoningChoices, ...]

    def __post_init__(self) -> None:
        if (type(self.chatgpt_project_id) is not str
                or not _SCOPE_ID.fullmatch(self.chatgpt_project_id)
                or type(self.model_choices) is not tuple or not self.model_choices
                or any(type(item) is not TrustedModelReasoningChoices
                       for item in self.model_choices)
                or len({item.model_id for item in self.model_choices})
                != len(self.model_choices)):
            raise ValueError("invalid trusted project reasoning catalog")


@dataclass(frozen=True, slots=True)
class TrustedAgentReasoningPolicy:
    owner_id: str
    project_id: str
    agent_id: str
    default_reasoning_effort: str
    allowed_reasoning_efforts: tuple[str, ...]

    def __post_init__(self) -> None:
        if (any(type(value) is not str or not _SCOPE_ID.fullmatch(value)
                for value in (self.owner_id, self.project_id, self.agent_id))
                or type(self.allowed_reasoning_efforts) is not tuple
                or not self.allowed_reasoning_efforts
                or any(type(effort) is not str or not _EFFORT_ID.fullmatch(effort)
                       for effort in self.allowed_reasoning_efforts)
                or len(set(self.allowed_reasoning_efforts))
                != len(self.allowed_reasoning_efforts)
                or type(self.default_reasoning_effort) is not str
                or self.default_reasoning_effort not in self.allowed_reasoning_efforts):
            raise ValueError("invalid trusted agent reasoning policy")


@dataclass(frozen=True, slots=True)
class ReasoningSelectionEvidence:
    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    profile_key: str
    mailbox_key: str
    model_id: str
    reasoning_effort: str
    verified: bool


class ProjectReasoningSelector(Protocol):
    def select(self, *, binding: TrustedChatGPTProjectBinding,
               profile: WebAgentProfile, model_id: str,
               reasoning_effort: str) -> ReasoningSelectionEvidence: ...
