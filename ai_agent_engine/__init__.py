"""A small, provider-neutral and explicitly permissioned agent runtime."""

from .core import (
    Action, Agent, AgentResult, AgentRole, AgentSession, Message, Model, ModelTurn,
    ScriptedModel, SessionContext, StopToken, Tool, ToolRegistry, AuditEvent,
)
from .tools import SandboxFiles, ToolError
from .model_routing import ModelRouter, ModelSetupError, ProviderSpec, TextOnlyModel
from .chatgpt_web import (
    BrowserPort, BrowserTurn, ChatGPTWebConfig, ChatGPTWebModel,
    ChatGPTWebUnavailable, chatgpt_web_provider_spec,
)
from .chatgpt_web_sessions import (
    ScopedBrowserPort, WebAgentIdentity, WebAgentProfile, WebAgentScopeError,
    WebAgentSessionRegistry,
)
from .chatgpt_web_projects import (
    ProjectMembershipEvidence, ProjectMembershipVerifier,
    TrustedChatGPTProjectBinding, TrustedChatGPTProjectRegistry,
)

__all__ = [
    "Action", "Agent", "AgentResult", "AgentRole", "AgentSession", "Message", "Model",
    "ModelTurn", "ScriptedModel", "SessionContext", "StopToken",
    "Tool", "ToolRegistry", "AuditEvent", "SandboxFiles", "ToolError",
    "ModelRouter", "ModelSetupError", "ProviderSpec", "TextOnlyModel",
    "BrowserPort", "BrowserTurn", "ChatGPTWebConfig", "ChatGPTWebModel",
    "ChatGPTWebUnavailable", "chatgpt_web_provider_spec",
    "ScopedBrowserPort", "WebAgentIdentity", "WebAgentProfile", "WebAgentScopeError",
    "WebAgentSessionRegistry",
    "ProjectMembershipEvidence", "ProjectMembershipVerifier",
    "TrustedChatGPTProjectBinding", "TrustedChatGPTProjectRegistry",
]
