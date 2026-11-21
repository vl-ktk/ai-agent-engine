"""WEB-04: trusted, immutable ChatGPT Project mapping and mock membership gate.

This is an offline verification contract, not a ChatGPT UI integration.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from .chatgpt_web_sessions import WebAgentScopeError

if TYPE_CHECKING:
    from .chatgpt_web_sessions import WebAgentProfile


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class TrustedChatGPTProjectBinding:
    """Owner-authorized internal project to one external Project ID."""

    owner_id: str
    project_id: str
    chatgpt_project_id: str

    def __post_init__(self) -> None:
        if any(type(value) is not str or not _ID.fullmatch(value)
               for value in (self.owner_id, self.project_id,
                             self.chatgpt_project_id)):
            raise ValueError("invalid trusted ChatGPT Project binding")


@dataclass(frozen=True, slots=True)
class ProjectMembershipEvidence:
    """Only a trusted verifier may produce evidence; untrusted fields are checked."""

    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    profile_key: str
    mailbox_key: str
    verified: bool


class ProjectMembershipVerifier(Protocol):
    """Injected verifier; WEB-04 supplies mock implementations only."""

    def verify(self, *, binding: TrustedChatGPTProjectBinding,
               profile: WebAgentProfile) -> ProjectMembershipEvidence: ...


class TrustedChatGPTProjectRegistry:
    """Fail-closed, immutable mappings: never infer IDs from user prompts."""

    def __init__(self, bindings: tuple[TrustedChatGPTProjectBinding, ...]):
        if type(bindings) is not tuple or not bindings:
            raise ValueError("trusted ChatGPT Project bindings required")
        by_scope: dict[tuple[str, str], TrustedChatGPTProjectBinding] = {}
        external: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, TrustedChatGPTProjectBinding):
                raise ValueError("invalid trusted ChatGPT Project binding")
            scope = (binding.owner_id, binding.project_id)
            if scope in by_scope or binding.chatgpt_project_id in external:
                raise ValueError("ambiguous trusted ChatGPT Project binding")
            by_scope[scope] = binding
            external.add(binding.chatgpt_project_id)
        self._bindings = MappingProxyType(by_scope)

    def binding_for(self, *, owner_id: str,
                    project_id: str) -> TrustedChatGPTProjectBinding:
        if (type(owner_id) is not str or type(project_id) is not str
                or not _ID.fullmatch(owner_id) or not _ID.fullmatch(project_id)):
            raise WebAgentScopeError("invalid web project scope")
        try:
            return self._bindings[(owner_id, project_id)]
        except KeyError:
            raise WebAgentScopeError("unknown trusted ChatGPT Project") from None

    def verify_member(self, *, profile: WebAgentProfile,
                      verifier: ProjectMembershipVerifier) -> None:
        """Require fresh, exact-scope proof before *every* model turn.

        Never trust the model prompt, a caller-supplied Project ID, or a
        browser profile namespace as proof of actual ChatGPT membership.
        """
        binding = self.binding_for(owner_id=profile.owner_id,
                                   project_id=profile.project_id)
        try:
            evidence = verifier.verify(binding=binding, profile=profile)
        except Exception:
            raise WebAgentScopeError("ChatGPT Project membership not verified") from None
        if (type(evidence) is not ProjectMembershipEvidence
                or evidence.owner_id != binding.owner_id
                or evidence.project_id != binding.project_id
                or evidence.chatgpt_project_id != binding.chatgpt_project_id
                or evidence.agent_id != profile.agent_id
                or evidence.profile_key != profile.profile_key
                or evidence.mailbox_key != profile.mailbox_key
                or evidence.verified is not True):
            raise WebAgentScopeError("ChatGPT Project membership not verified")
