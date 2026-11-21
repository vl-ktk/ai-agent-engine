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
from .chatgpt_web_instructions import (
    TrustedAgentInstructionPolicy, WebInstructionBootstrap,
)
from .chatgpt_web_reasoning import (
    ProjectReasoningSelector, ReasoningSelectionEvidence,
    TrustedAgentReasoningPolicy, TrustedProjectReasoningCatalog,
    WebReasoningSelectionError,
)
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
                 selector: ProjectModelSelector,
                 reasoning_catalogs: tuple[TrustedProjectReasoningCatalog, ...] | None = None,
                 reasoning_selector: ProjectReasoningSelector | None = None,
                 instruction_bootstrap: WebInstructionBootstrap | None = None):
        if not isinstance(sessions, WebAgentSessionRegistry):
            raise TypeError("invalid web agent session registry")
        if (type(project_catalogs) is not tuple or not project_catalogs
                or not callable(getattr(selector, "select", None))):
            raise ValueError("trusted model catalog and selector required")
        if (instruction_bootstrap is not None
                and not isinstance(instruction_bootstrap, WebInstructionBootstrap)):
            raise ValueError("invalid trusted instruction bootstrap")
        projects: dict[str, TrustedProjectModelCatalog] = {}
        for catalog in project_catalogs:
            if (not isinstance(catalog, TrustedProjectModelCatalog)
                    or catalog.chatgpt_project_id in projects):
                raise ValueError("ambiguous project model catalog")
            projects[catalog.chatgpt_project_id] = catalog
        if (reasoning_catalogs is None) != (reasoning_selector is None):
            raise ValueError("trusted reasoning catalog and selector must be paired")
        reasoning_projects: dict[str, dict[str, tuple[str, ...]]] = {}
        if reasoning_catalogs is not None:
            if (type(reasoning_catalogs) is not tuple or not reasoning_catalogs
                    or not callable(getattr(reasoning_selector, "select", None))):
                raise ValueError("invalid trusted reasoning catalog or selector")
            for reasoning_catalog in reasoning_catalogs:
                if (not isinstance(reasoning_catalog, TrustedProjectReasoningCatalog)
                        or reasoning_catalog.chatgpt_project_id in reasoning_projects):
                    raise ValueError("ambiguous project reasoning catalog")
                project = projects.get(reasoning_catalog.chatgpt_project_id)
                if (project is None
                        or any(choice.model_id not in project.model_ids
                               for choice in reasoning_catalog.model_choices)):
                    raise ValueError("reasoning catalog contains unavailable model")
                reasoning_projects[reasoning_catalog.chatgpt_project_id] = {
                    choice.model_id: choice.reasoning_efforts
                    for choice in reasoning_catalog.model_choices
                }
        self._sessions = sessions
        self._projects = MappingProxyType(projects)
        self._selector = selector
        self._reasoning_projects = MappingProxyType({
            project: MappingProxyType(choices)
            for project, choices in reasoning_projects.items()
        })
        self._reasoning_selector = reasoning_selector
        self._instruction_bootstrap = instruction_bootstrap
        self._lock = Lock()
        self._policies: dict[str, TrustedAgentModelPolicy] = {}
        self._reasoning_policies: dict[str, TrustedAgentReasoningPolicy] = {}

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

    def register_reasoning(self, policy: TrustedAgentReasoningPolicy) -> None:
        """WEB-07: a pinned agent policy; requires existing WEB-06 registration."""
        if not isinstance(policy, TrustedAgentReasoningPolicy):
            raise TypeError("invalid trusted agent reasoning policy")
        if self._reasoning_selector is None:
            raise WebReasoningSelectionError("web reasoning selection is disabled")
        model_policy = self._policy(owner_id=policy.owner_id,
                                    project_id=policy.project_id,
                                    agent_id=policy.agent_id)
        binding = self._sessions.project_binding(
            owner_id=policy.owner_id, project_id=policy.project_id,
            agent_id=policy.agent_id)
        choices = self._reasoning_projects.get(binding.chatgpt_project_id)
        if (choices is None
                or policy.default_reasoning_effort not in
                choices.get(model_policy.default_model_id, ())
                or any(not any(effort in choices.get(model, ())
                                   for model in model_policy.allowed_model_ids)
                       for effort in policy.allowed_reasoning_efforts)):
            raise WebReasoningSelectionError("unavailable trusted web reasoning effort")
        with self._lock:
            if policy.agent_id in self._reasoning_policies:
                raise WebReasoningSelectionError("agent reasoning policy already registered")
            self._reasoning_policies[policy.agent_id] = policy

    def register_instructions(self, policy: TrustedAgentInstructionPolicy) -> None:
        """WEB-08: pin the revision after the model and agent are registered."""
        if not isinstance(policy, TrustedAgentInstructionPolicy):
            raise TypeError("invalid trusted instruction policy")
        if self._instruction_bootstrap is None:
            raise WebAgentScopeError("web instruction bootstrap disabled")
        self._policy(owner_id=policy.owner_id, project_id=policy.project_id,
                     agent_id=policy.agent_id)
        self._instruction_bootstrap.register(
            policy,
            binding=self._sessions.project_binding(
                owner_id=policy.owner_id, project_id=policy.project_id,
                agent_id=policy.agent_id),
            identity=self._sessions.identity(
                owner_id=policy.owner_id, project_id=policy.project_id,
                agent_id=policy.agent_id))

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
            prompt: str, requested_model_id: str | None = None,
            requested_reasoning_effort: str | None = None) -> AgentResult:
        model_id = self.selected_model(
            owner_id=owner_id, project_id=project_id, agent_id=agent_id,
            requested_model_id=requested_model_id)
        binding = self._sessions.project_binding(
            owner_id=owner_id, project_id=project_id, agent_id=agent_id)

        effort: str | None = None
        if self._reasoning_selector is not None:
            with self._lock:
                reasoning_policy = self._reasoning_policies.get(agent_id)
            if (reasoning_policy is None or reasoning_policy.owner_id != owner_id
                    or reasoning_policy.project_id != project_id):
                raise WebReasoningSelectionError("agent reasoning policy not registered")
            effort = (reasoning_policy.default_reasoning_effort
                      if requested_reasoning_effort is None
                      else requested_reasoning_effort)
            choices = self._reasoning_projects.get(binding.chatgpt_project_id, {})
            if (type(effort) is not str
                    or effort not in reasoning_policy.allowed_reasoning_efforts
                    or effort not in choices.get(model_id, ())):
                raise WebReasoningSelectionError("unavailable trusted web reasoning effort")
        elif requested_reasoning_effort is not None:
            raise WebReasoningSelectionError("web reasoning selection is disabled")

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
            if effort is not None:
                try:
                    reasoning_proof = self._reasoning_selector.select(
                        binding=binding, profile=profile, model_id=selected,
                        reasoning_effort=effort)
                except Exception:
                    raise WebReasoningSelectionError(
                        "web reasoning selection unverified") from None
                if (type(reasoning_proof) is not ReasoningSelectionEvidence
                        or reasoning_proof.verified is not True
                        or reasoning_proof.owner_id != profile.owner_id
                        or reasoning_proof.project_id != profile.project_id
                        or reasoning_proof.agent_id != profile.agent_id
                        or reasoning_proof.chatgpt_project_id != binding.chatgpt_project_id
                        or reasoning_proof.profile_key != profile.profile_key
                        or reasoning_proof.mailbox_key != profile.mailbox_key
                        or reasoning_proof.model_id != selected
                        or reasoning_proof.reasoning_effort != effort):
                    raise WebReasoningSelectionError("web reasoning selection unverified")

        def load_instructions(profile: WebAgentProfile) -> str:
            return self._instruction_bootstrap.load(
                binding=binding, profile=profile,
                identity=self._sessions.identity(
                    owner_id=owner_id, project_id=project_id, agent_id=agent_id))

        return self._sessions.run_with_model_verifier(
            owner_id=owner_id, project_id=project_id, agent_id=agent_id,
            model_id=model_id, prompt=prompt, model_verifier=verify,
            instructions_loader=(load_instructions
                                 if self._instruction_bootstrap is not None else None))
