"""Opt-in ONE owner-subscribed Claude task acceptance, never run by CI.

Execute only in the owner's already signed-in Codespace. It uses personal
plan allowance, an ephemeral SQLite database, and no paid API credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from .task_api import OwnerScope, TaskInbox, parse_submission
from .task_assignment import WorkspaceRoot
from .task_claude import ClaudeTaskExecutor
from .task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from .task_state import TaskState
from .task_storage import SQLiteTaskStore


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ae-claude-63-") as temp:
        root = Path(temp)
        slots = root / "slots"
        slots.mkdir(mode=0o700)
        (slots / "owner-a").mkdir(mode=0o700)
        scope = OwnerScope("personal-claude-owner", frozenset({"private-test"}))
        db_path = root / "tasks.sqlite3"
        inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                          workspace_root=WorkspaceRoot(slots))
        runner = None
        try:
            task_id = inbox.accept(parse_submission(json.dumps({
                "project_id": "private-test", "title": "One Claude Pro task",
                "prompt": "Answer with the exact marker AE-63-OK.",
            }).encode()), scope).task_id
            gate = TaskPermissionGate(inbox, grants=(
                ActionGrant(task_id, "claude-worker",
                            ProtectedAction.EXTERNAL_REQUEST),))
            runner = ClaudeTaskExecutor(
                inbox, scope, authorized_owner_id="personal-claude-owner",
                worker_id="claude-worker", model_id="default",
                permission_gate=gate, timeout_seconds=210)
            result = runner.execute(task_id, "owner-a")
            if (result.state is not TaskState.SUCCEEDED
                    or not result.text or "AE-63-OK" not in result.text
                    or runner.delivery.snapshot(task_id, scope).status != "completed"
                    or [e.provider for e in runner.provider_history(task_id)] != ["claude"]
                    or [e.outcome for e in runner.provider_history(task_id)] != ["completed"]
                    or [entry.allowed for entry in gate.audit_history(task_id, scope)]
                    != [True]):
                print("CLAUDE_TASK_6_3_LIVE FAIL: task/authorization/ack")
                return 2
            runner.close()
            inbox.close()
            inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                              workspace_root=WorkspaceRoot(slots))
            if inbox.state(task_id, scope) is not TaskState.SUCCEEDED:
                print("CLAUDE_TASK_6_3_LIVE FAIL: durable replay")
                return 2
            print("CLAUDE_TASK_6_3_LIVE PASS: one personal Claude Pro task, "
                  "permission, result, provider audit and SQLite restart")
            return 0
        except Exception:
            # Never display model output, keys, OAuth details or task contents.
            print("CLAUDE_TASK_6_3_LIVE BLOCKED: private diagnostic required")
            return 2
        finally:
            if runner is not None:
                runner.close()
            inbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
