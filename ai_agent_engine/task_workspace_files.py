"""Stage 6.6: file tools bound to ONE trusted task assignment and workspace.

The model may only supply relative file arguments. It cannot select owner,
task, worker, workspace, or authorize an operation through its prompt/JSON.
Each tool call is checked by the durable TaskPermissionGate before I/O.

This is tool-level path confinement, NOT a Linux process mount/seccomp sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from .core import Tool
from .task_api import OwnerScope, TaskInbox
from .task_assignment import TaskAssignment, validate_worker
from .task_permissions import ProtectedAction, TaskPermissionGate
from .tools import SandboxFiles, ToolError


class TaskWorkspaceFiles:
    """Two bounded, grant-gated tools using a pre-existing assigned folder."""

    def __init__(self, inbox: TaskInbox, scope: OwnerScope, *,
                 task_id: str, worker_id: str, gate: TaskPermissionGate,
                 max_bytes: int = 8192) -> None:
        if (not isinstance(inbox, TaskInbox) or not inbox.durable
                or not isinstance(scope, OwnerScope)
                or not isinstance(gate, TaskPermissionGate)
                or gate.inbox is not inbox):
            raise ToolError("trusted task and permission gate required")
        validate_worker(worker_id)
        assignment = inbox.get_assignment(task_id, scope)
        if assignment is None or assignment.worker_id != worker_id:
            raise ToolError("task has no matching workspace assignment")
        # No insecure platform fallback for the task-bound file tools.
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise ToolError("secure workspace tools unavailable")
        self._inbox = inbox
        self._scope = scope
        self._task_id = task_id
        self._worker_id = worker_id
        self._assignment: TaskAssignment = assignment
        self._gate = gate
        try:
            self._files = SandboxFiles(Path(assignment.workspace_dir),
                                       max_bytes=max_bytes)
        except ValueError:
            raise ToolError("assigned workspace unavailable") from None

    def _verify_assignment(self) -> None:
        current = self._inbox.get_assignment(self._task_id, self._scope)
        if current is None or current != self._assignment:
            raise ToolError("workspace assignment changed")

    def read_text(self, args: Mapping[str, str]) -> str:
        """A non-secret workspace read needs EXECUTE_TOOL; no implicit grants."""
        if not isinstance(args, Mapping) or set(args) != {"path"}:
            raise ToolError("invalid file tool arguments")
        self._verify_assignment()
        return self._gate.run(
            self._task_id, self._scope, actor_id=self._worker_id,
            action=ProtectedAction.EXECUTE_TOOL,
            operation=lambda: self._files.read_text(args),
        )

    def write_new(self, args: Mapping[str, str]) -> str:
        """New-file-only write needs BOTH EXECUTE_TOOL and WRITE_WORKSPACE."""
        if not isinstance(args, Mapping) or set(args) != {"path", "content"}:
            raise ToolError("invalid file tool arguments")
        self._verify_assignment()

        def authorized_write() -> str:
            return self._gate.run(
                self._task_id, self._scope, actor_id=self._worker_id,
                action=ProtectedAction.WRITE_WORKSPACE,
                operation=lambda: self._files.write_new(args),
            )

        return self._gate.run(
            self._task_id, self._scope, actor_id=self._worker_id,
            action=ProtectedAction.EXECUTE_TOOL,
            operation=authorized_write,
        )

    def allowed_tools(self) -> tuple[Tool, Tool]:
        """Trusted registry wiring; tool arguments never select workspace."""
        return (
            Tool("write_new_file", self.write_new,
                 required_args=("path", "content")),
            Tool("read_file", self.read_text, required_args=("path",)),
        )
