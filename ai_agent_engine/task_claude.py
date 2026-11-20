"""Owner-bound Claude Pro task adapter using the existing durable GPT path.

This is an explicitly invoked, single-task adapter. It does not install a
daemon or authorize unattended execution on a hosted controller.
"""

from __future__ import annotations

import os
import sys

from .task_api import OwnerScope, TaskInbox
from .task_gpt import GPTTaskExecutor
from .main_shared_capacity import SharedCapacity
from .task_permissions import TaskPermissionGate


class ClaudeTaskExecutor(GPTTaskExecutor):
    """Strict personal owner scope and official subscription CLI only."""

    _provider = "claude"

    def __init__(self, inbox: TaskInbox, scope: OwnerScope, *,
                 authorized_owner_id: str, worker_id: str,
                 permission_gate: TaskPermissionGate,
                 model_id: str = "default", timeout_seconds: float = 180,
                 worker_argv: tuple[str, ...] | None = None,
                 capacity: SharedCapacity | None = None) -> None:
        if (type(authorized_owner_id) is not str or not authorized_owner_id
                or not isinstance(scope, OwnerScope)
                or scope.owner_id != authorized_owner_id):
            raise ValueError("Claude subscription belongs to another owner")
        super().__init__(inbox, scope, worker_id=worker_id,
                         model_id=model_id, permission_gate=permission_gate,
                         timeout_seconds=timeout_seconds,
                         capacity=capacity,
                         worker_argv=worker_argv or (
                             sys.executable, "-m", "ai_agent_engine.claude_task_worker"))

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        environment = GPTTaskExecutor._worker_environment()
        for key in tuple(environment):
            upper = key.upper()
            if (upper.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE"))
                    or upper in ("NODE_OPTIONS", "NODE_PATH")):
                environment.pop(key)
        return environment
