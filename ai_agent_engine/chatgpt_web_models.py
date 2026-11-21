"""WEB-06: explicit per-agent/per-task model policy and mock selection proof.

Configured catalog and injected selector are NOT proof of live UI models.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from threading import Lock
from types import MappingProxyType
from typing import Protocol

from .chatgpt_web_projects import TrustedChatGPTProjectBinding
from .chatgpt_web_sessions import (
    WebAgentProfile, WebAgentScopeError, WebAgentSessionRegistry,
)
from .core import AgentResult


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class WebModelSelectionError(WebAgentScopeError):
    """Sanitized fail-closed model policy/selection refusal."""


@dataclass(frozen=True, slots=True)
class TrustedProjectModelCatalog:
    """Owner-supplied available mock model IDs for one ChatGPT Project."""

    chatgpt_project_id: str
    model_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (type(self.chatgpt_project_id) is not str
                or not _SCOPE_ID.fullmatch(self.chatgpt_project_id)
                or type(self.model_ids) is not tuple or not self.model_ids
                or any(type(model) is not str or not _MODEL_ID.fullmatch(model)
                       for model in self.model_ids)
                or len(set(self.model_ids)) != len(self.model_ids)):
            raise ValueError("invalid trusted project model catalog")


@dataclass(frozen=True, slots=True)
class TrustedAgentModelPolicy:
    owner_id: str
    project_id: str
    agent_id: str
    default_model_id: str
    allowed_model_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (any(type(x) is not str or not _SCOPE_ID.fullmatch(x)
                for x in (self.owner_id, self.project_id, self.agent_id))
                or type(self.allowed_model_ids) is not tuple
                or not self.allowed_model_ids
                or any(type(x) is not str or not _MODEL_ID.fullmatch(x)
                       for x in self.allowed_model_ids)
                or len(set(self.allowed_model_ids)) != len(self.allowed_model_ids)
                or type(self.default_model_id) is not str
                or self.default_model_id not in self.allowed_model_ids):
            raise ValueError("invalid trusted agent model policy")


@dataclass(frozen=True, slots=True)
class ModelSelectionEvidence:
    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    profile_key: str
    mailbox_key: str
    model_id: str
    verified: bool


class ProjectModelSelector(Protocol):
    def select(self, *, binding: TrustedChatGPTProjectBinding,
               profile: WebAgentProfile,
               model_id: str) -> ModelSelectionEvidence: ...


class WebAgentModelRegistry:
    """Immutable agent model choices, explicit task overrides, no fallback.

    One trusted registry per process. Checks a mock selector inside the
    session's FIFO immediately before BrowserPort.complete is permitted.
    """

    def __init__(self, sessions: WebAgentSessionRegistry, *,
                 project_catalogs: tuple[TrustedProjectModelCatalog, ...],
                 selector: ProjectModelSelector):
        if not isinstance(sessions, WebAgentSessionRegistry):
            raise TypeError("invalid web agent session registry")
        if (type(project_catalogs) is not tuple or not project_catalogs
                or not callable(getattr(selector, "select", None))):
            raise ValueError("trusted model catalog and selector required")
        projects: dict[str, TrustedProjectModelCatalog] = {}
        for catalog in project_catalogs:
            if (not isinstance(catalog, TrustedProjectModelCatalog)
                    or catalog.chatgpt_project_id in projects):
                raise ValueError("ambiguous project model catalog")
            projects[catalog.chatgpt_project_id] = catalog
        self._sessions = sessions
        self._projects = MappingProxyType(projects)
        self._selector = selector
        self._lock = Lock()
        self._policies: dict[str, TrustedAgentModelPolicy] = {}

    def register(self, policy: TrustedAgentModelPolicy) -> None:
        if not isinstance(policy, TrustedAgentModelPolicy):
            raise TypeError("invalid trusted agent model policy")
        # WEB-04 gate must be present; registration is strictly trusted.
        binding = self._sessions.project_binding(
            owner_id=policy.owner_id, project_id=policy.project_id,
            agent_id=policy.agent_id)
        catalog = self._projects.get(binding.chatgpt_project_id)
        if (catalog is None
                or any(model not in catalog.model_ids
                       or model not in self._sessions.configured_models
                       for model in policy.allowed_model_ids)):
            raise WebModelSelectionError("unavailable trusted web model")
        with self._lock:
            if policy.agent_id in self._policies:
                raise WebModelSelectionError("agent model policy already registered")
            self._policies[policy.agent_id] = policy

    def _policy(self, *, owner_id: str, project_id: str,
                agent_id: str) -> TrustedAgentModelPolicy:
        self._sessions.profile(owner_id=owner_id, project_id=project_id,
                               agent_id=agent_id)
        with self._lock:
            policy = self._policies.get(agent_id)
        if (policy is None or policy.owner_id != owner_id
                or policy.project_id != project_id):
            raise WebModelSelectionError("agent model policy not registered")
        return policy

    def selected_model(self, *, owner_id: str, project_id: str,
                       agent_id: str,
                       requested_model_id: str | None = None) -> str:
        policy = self._policy(owner_id=owner_id, project_id=project_id,
                              agent_id=agent_id)
        model_id = (policy.default_model_id if requested_model_id is None
                    else requested_model_id)
        if type(model_id) is not str or model_id not in policy.allowed_model_ids:
            raise WebModelSelectionError("unavailable trusted web model")
        return model_id

    def run(self, *, owner_id: str, project_id: str, agent_id: str,
            prompt: str, requested_model_id: str | None = None) -> AgentResult:
        model_id = self.selected_model(
            owner_id=owner_id, project_id=project_id, agent_id=agent_id,
            requested_model_id=requested_model_id)
        binding = self._sessions.project_binding(
            owner_id=owner_id, project_id=project_id, agent_id=agent_id)

        def verify(profile: WebAgentProfile, selected: str) -> None:
            try:
                proof = self._selector.select(binding=binding, profile=profile,
                                              model_id=selected)
            except Exception:
                raise WebModelSelectionError("web model selection unverified") from None
            if (type(proof) is not ModelSelectionEvidence
                    or proof.verified is not True
                    or proof.owner_id != profile.owner_id
                    or proof.project_id != profile.project_id
                    or proof.agent_id != profile.agent_id
                    or proof.chatgpt_project_id != binding.chatgpt_project_id
                    or proof.profile_key != profile.profile_key
                    or proof.mailbox_key != profile.mailbox_key
                    or proof.model_id != selected):
                raise WebModelSelectionError("web model selection unverified")

        return self._sessions.run_with_model_verifier(
            owner_id=owner_id, project_id=project_id, agent_id=agent_id,
            model_id=model_id, prompt=prompt, model_verifier=verify)
