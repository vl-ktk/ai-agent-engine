"""WEB-08: pinned project instructions from an injected scoped mock source.

The source and grant verifier are trusted test ports, NOT a real database,
ChatGPT Project instruction API, or authorization to perform actions.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from threading import Lock
from typing import Protocol

from .chatgpt_web_projects import TrustedChatGPTProjectBinding
from .chatgpt_web_sessions import (
    WebAgentIdentity, WebAgentProfile, WebAgentScopeError,
)
from .core import AgentRole


_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ACTION_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class WebInstructionBootstrapError(WebAgentScopeError):
    """Sanitized refusal to load or use scoped project instructions."""


@dataclass(frozen=True, slots=True)
class TrustedAgentInstructionPolicy:
    """Owner-pinned active revision/digest, role and required grant proof."""

    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    role: AgentRole
    revision: int
    instructions_sha256: str
    required_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (any(type(x) is not str or not _SCOPE_ID.fullmatch(x)
                for x in (self.owner_id, self.project_id, self.agent_id,
                          self.chatgpt_project_id))
                or type(self.role) is not AgentRole
                or type(self.revision) is not int or self.revision < 1
                or type(self.instructions_sha256) is not str
                or not _SHA256.fullmatch(self.instructions_sha256)
                or type(self.required_actions) is not tuple
                or any(type(action) is not str
                       or not _ACTION_NAME.fullmatch(action)
                       for action in self.required_actions)
                or len(set(self.required_actions)) != len(self.required_actions)):
            raise ValueError("invalid trusted instruction policy")


@dataclass(frozen=True, slots=True)
class ScopedInstructionSnapshot:
    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    profile_key: str
    mailbox_key: str
    role: AgentRole
    revision: int
    instructions: str
    current: bool
    verified: bool


@dataclass(frozen=True, slots=True)
class InstructionGrantEvidence:
    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    profile_key: str
    mailbox_key: str
    role: AgentRole
    revision: int
    required_actions: tuple[str, ...]
    verified: bool


class ScopedInstructionSource(Protocol):
    def read_current(self, *, binding: TrustedChatGPTProjectBinding,
                     profile: WebAgentProfile,
                     role: AgentRole) -> ScopedInstructionSnapshot: ...


class InstructionGrantVerifier(Protocol):
    def verify(self, *, binding: TrustedChatGPTProjectBinding,
               profile: WebAgentProfile, role: AgentRole,
               revision: int,
               required_actions: tuple[str, ...]) -> InstructionGrantEvidence: ...


class WebInstructionBootstrap:
    """In-memory revision policies; fresh mock source and grant check per turn.

    Text from memory is never allowed to declare new grants. The verifier is
    a *mock proof* of exact actions, not TaskPermissionGate integration.
    """

    def __init__(self, *, source: ScopedInstructionSource,
                 grant_verifier: InstructionGrantVerifier):
        if (not callable(getattr(source, "read_current", None))
                or not callable(getattr(grant_verifier, "verify", None))):
            raise ValueError("trusted scoped source and grant verifier required")
        self._source = source
        self._grant_verifier = grant_verifier
        self._lock = Lock()
        self._policies: dict[str, TrustedAgentInstructionPolicy] = {}

    def register(self, policy: TrustedAgentInstructionPolicy, *,
                 binding: TrustedChatGPTProjectBinding,
                 identity: WebAgentIdentity) -> None:
        if not isinstance(policy, TrustedAgentInstructionPolicy):
            raise TypeError("invalid trusted instruction policy")
        if (not isinstance(binding, TrustedChatGPTProjectBinding)
                or not isinstance(identity, WebAgentIdentity)
                or (policy.owner_id, policy.project_id, policy.agent_id,
                    policy.chatgpt_project_id) !=
                   (identity.owner_id, identity.project_id, identity.agent_id,
                    binding.chatgpt_project_id)
                or (binding.owner_id, binding.project_id) !=
                   (identity.owner_id, identity.project_id)
                or policy.role is not identity.role
                or any(action not in identity.declared_actions
                       for action in policy.required_actions)
                or identity.instructions):
            raise WebInstructionBootstrapError("trusted instruction scope mismatch")
        with self._lock:
            if policy.agent_id in self._policies:
                raise WebInstructionBootstrapError("instruction policy already registered")
            self._policies[policy.agent_id] = policy

    def load(self, *, binding: TrustedChatGPTProjectBinding,
             profile: WebAgentProfile, identity: WebAgentIdentity) -> str:
        with self._lock:
            policy = self._policies.get(profile.agent_id)
        if (policy is None
                or (policy.owner_id, policy.project_id, policy.agent_id,
                    policy.chatgpt_project_id) !=
                   (profile.owner_id, profile.project_id, profile.agent_id,
                    binding.chatgpt_project_id)
                or (binding.owner_id, binding.project_id) !=
                   (profile.owner_id, profile.project_id)
                or (identity.owner_id, identity.project_id, identity.agent_id) !=
                   (profile.owner_id, profile.project_id, profile.agent_id)
                or identity.role is not policy.role):
            raise WebInstructionBootstrapError("instruction scope unverified")

        try:
            snapshot = self._source.read_current(
                binding=binding, profile=profile, role=policy.role)
        except Exception:
            raise WebInstructionBootstrapError("project instructions unavailable") from None
        if (type(snapshot) is not ScopedInstructionSnapshot
                or snapshot.verified is not True or snapshot.current is not True
                or (snapshot.owner_id, snapshot.project_id, snapshot.agent_id,
                    snapshot.chatgpt_project_id, snapshot.profile_key,
                    snapshot.mailbox_key) !=
                   (profile.owner_id, profile.project_id, profile.agent_id,
                    binding.chatgpt_project_id, profile.profile_key,
                    profile.mailbox_key)
                or snapshot.role is not policy.role
                or type(snapshot.revision) is not int
                or snapshot.revision != policy.revision
                or type(snapshot.instructions) is not str
                or not snapshot.instructions.strip()
                or "\x00" in snapshot.instructions
                or len(snapshot.instructions) > 8_000
                or hashlib.sha256(snapshot.instructions.encode("utf-8")).hexdigest()
                   != policy.instructions_sha256):
            raise WebInstructionBootstrapError("project instructions unverified")

        try:
            grant = self._grant_verifier.verify(
                binding=binding, profile=profile, role=policy.role,
                revision=policy.revision, required_actions=policy.required_actions)
        except Exception:
            raise WebInstructionBootstrapError("instruction grant unverified") from None
        if (type(grant) is not InstructionGrantEvidence
                or grant.verified is not True
                or (grant.owner_id, grant.project_id, grant.agent_id,
                    grant.chatgpt_project_id, grant.profile_key,
                    grant.mailbox_key) !=
                   (profile.owner_id, profile.project_id, profile.agent_id,
                    binding.chatgpt_project_id, profile.profile_key,
                    profile.mailbox_key)
                or grant.role is not policy.role
                or type(grant.revision) is not int
                or grant.revision != policy.revision
                or type(grant.required_actions) is not tuple
                or grant.required_actions != policy.required_actions):
            raise WebInstructionBootstrapError("instruction grant unverified")

        return snapshot.instructions

    def pinned_policy(self, *, owner_id: str, project_id: str,
                      agent_id: str) -> TrustedAgentInstructionPolicy:
        """Trusted metadata only; never returns memory or grants as authority."""
        with self._lock:
            policy = self._policies.get(agent_id)
        if (policy is None or (policy.owner_id, policy.project_id,
                              policy.agent_id) !=
                             (owner_id, project_id, agent_id)):
            raise WebInstructionBootstrapError("instruction policy unavailable")
        return policy
