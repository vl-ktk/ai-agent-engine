"""Owner-triggered ONE real SIWC task→GPT→SQLite acceptance in a temp scope.

Never run in CI or unattended: one ChatGPT-plan request may refresh the
existing owner OAuth token and count against account limits. No API keys.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from .task_api import OwnerScope, TaskInbox, parse_submission
from .task_assignment import WorkspaceRoot
from .task_gpt import GPTTaskExecutor
from .task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from .task_state import TaskState
from .task_storage import SQLiteTaskStore


def main() -> int:
    """Use only previously authorized plan access; do not silently retry."""
    with tempfile.TemporaryDirectory(prefix="ae-gpt-62-") as temp:
        root = Path(temp)
        slots = root / "slots"
        slots.mkdir(mode=0o700)
        (slots / "worker-a").mkdir(mode=0o700)
        db_path = root / "tasks.sqlite3"
        scope = OwnerScope("owner-live", frozenset({"engine-live-test"}))
        inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                          workspace_root=WorkspaceRoot(slots))
        runner = None
        try:
            submission = parse_submission(json.dumps({
                "project_id": "engine-live-test",
                "title": "Scoped GPT task 6.2 single live acceptance",
                "prompt": "Return the exact marker AE-62-OK in your answer.",
            }).encode())
            task_id = inbox.accept(submission, scope).task_id
            gate = TaskPermissionGate(inbox, grants=(
                ActionGrant(task_id, "gpt-worker",
                            ProtectedAction.EXTERNAL_REQUEST),))
            runner = GPTTaskExecutor(inbox, scope, worker_id="gpt-worker",
                                     model_id="gpt-6.1-sol", permission_gate=gate,
                                     timeout_seconds=90)
            result = runner.execute(task_id, "worker-a")
            if (result.state is not TaskState.SUCCEEDED
                    or not result.text or "AE-62-OK" not in result.text
                    or runner.delivery.snapshot(task_id, scope).status != "completed"
                    or [a.allowed for a in gate.audit_history(task_id, scope)] != [True]
                    or [e.outcome for e in runner.provider_history(task_id)] !=
                    ["completed"]):
                print("GPT_TASK_6_2_LIVE FAIL: result/authorization/ack")
                return 2
            runner.close()
            inbox.close()
            inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                              workspace_root=WorkspaceRoot(slots))
            if inbox.state(task_id, scope) is not TaskState.SUCCEEDED:
                print("GPT_TASK_6_2_LIVE FAIL: durable replay")
                return 2
            print("GPT_TASK_6_2_LIVE PASS: one scoped GPT response, "
                  "permission, completion, audit and SQLite restart")
            return 0
        except Exception:
            # No provider exception text, prompts, paths, or auth context.
            print("GPT_TASK_6_2_LIVE BLOCKED: see local private diagnostics")
            return 2
        finally:
            if runner is not None:
                runner.close()
            inbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
