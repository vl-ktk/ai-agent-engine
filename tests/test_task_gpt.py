"""6.2: private SIWC worker ↔ real Popen ↔ delivery ↔ owner/worker/grants.

The only network-capable production path is NOT invoked by this test file.
All spawned model processes are disposable local deterministic fixtures.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine.gpt_task_worker import execute_request, _safe_error_category
from ai_agent_engine.api_errors import PlanRequestError
from ai_agent_engine.task_api import OwnerScope, TaskInbox, TaskNotFound, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_gpt import GPTTaskError, GPTTaskExecutor
from ai_agent_engine.task_permissions import (
    ActionGrant, PermissionDenied, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore, StorageUnavailable


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("owner-b", frozenset({"project-a"}))
MODEL = "gpt-test-model"

ECHO_CODE = (
    "import json,sys; "
    "r=json.loads(sys.stdin.buffer.read()); "
    "print(json.dumps({'ok':True,'text':'offline:'+r['prompt']}),flush=True)"
)
FAIL_CODE = "import sys;sys.stdin.buffer.read();raise SystemExit(2)"
HANG_CODE = "import sys,time;sys.stdin.buffer.read();time.sleep(60)"
MALFORMED_CODE = "import sys;sys.stdin.buffer.read();print('not-json')"
ENV_CODE = (
    "import os,sys,json;sys.stdin.buffer.read();"
    "print(json.dumps({'ok':True,'text':str(os.getenv('OPENAI_API_KEY') is None)}))"
)


class GPTTaskOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-62-")
        self.root_dir = Path(self.temp.name)
        ws = self.root_dir / "workspaces"
        ws.mkdir(mode=0o700)
        for name in ("slot-a", "slot-b"):
            (ws / name).mkdir(mode=0o700)
        self.root = WorkspaceRoot(ws)
        self.db_path = self.root_dir / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db_path),
                               workspace_root=self.root)
        self.executors: list[GPTTaskExecutor] = []

    def tearDown(self):
        try:
            for executor in self.executors:
                executor.close()
        finally:
            self.inbox.close()
            self.temp.cleanup()

    def new_task(self, *, scope=OWNER, prompt="A private offline request"):
        return self.inbox.accept(parse_submission(json.dumps({
            "project_id": "project-a", "title": "Mock GPT task", "prompt": prompt,
        }).encode()), scope).task_id

    def executor(self, task_id, *, scope=OWNER, grant=True, command=ECHO_CODE,
                 timeout_seconds=3):
        grants = (ActionGrant(task_id, "worker-a",
                              ProtectedAction.EXTERNAL_REQUEST),) if grant else ()
        gate = TaskPermissionGate(self.inbox, grants=grants)
        worker = GPTTaskExecutor(
            self.inbox, scope, worker_id="worker-a", model_id=MODEL,
            permission_gate=gate, timeout_seconds=timeout_seconds,
            worker_argv=(sys.executable, "-u", "-c", command))
        self.executors.append(worker)
        return worker, gate

    def test_real_private_child_commits_permission_then_success_and_ack(self):
        task_id = self.new_task(prompt="Hello! Привет")
        runner, gate = self.executor(task_id)
        result = runner.execute(task_id, "slot-a")
        self.assertEqual((result.state, result.executed), (TaskState.SUCCEEDED, True))
        self.assertEqual(result.text, "offline:Hello! Привет")
        self.assertEqual(runner.delivery.snapshot(task_id, OWNER).status, "completed")
        self.assertEqual([entry.allowed for entry in gate.audit_history(task_id, OWNER)],
                         [True])
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.SUCCEEDED)
        self.assertEqual([e.outcome for e in runner.provider_history(task_id)],
                         ["completed"])
        self.assertFalse(runner.execute(task_id, "slot-a").executed)
        self.assertEqual(len(gate.audit_history(task_id, OWNER)), 1)

    def test_provider_result_masks_known_host_secret_without_audit_leak(self):
        task_id = self.new_task()
        secret = "fixture-owner-private-session-123456"
        command = (
            "import json,sys;sys.stdin.buffer.read();"
            f"print(json.dumps({{'ok':True,'text':{secret!r}}}),flush=True)"
        )
        runner, _gate = self.executor(task_id, command=command)
        with patch.dict(os.environ, {"SESSION_TOKEN": secret}):
            result = runner.execute(task_id, "slot-a")
        self.assertEqual(result.text, "[REDACTED]")
        self.assertEqual([e.outcome for e in runner.provider_history(task_id)],
                         ["completed"])

    def test_32_duplicate_attempts_issue_one_runnable_process(self):
        task_id = self.new_task()
        runner, gate = self.executor(task_id)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: runner.execute(task_id, "slot-a"),
                                    range(32)))
        self.assertEqual(sum(result.executed for result in results), 1)
        self.assertTrue(all(result.state in (TaskState.ASSIGNED,
                                              TaskState.RUNNING, TaskState.SUCCEEDED)
                            for result in results))
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.SUCCEEDED)
        self.assertEqual(runner.delivery.snapshot(task_id, OWNER).attempt, 1)
        self.assertEqual(len(gate.audit_history(task_id, OWNER)), 1)

    def test_missing_external_request_grant_never_delivers_payload(self):
        task_id = self.new_task()
        runner, gate = self.executor(task_id, grant=False)
        with self.assertRaises(PermissionDenied):
            runner.execute(task_id, "slot-a")
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)
        audits = gate.audit_history(task_id, OWNER)
        self.assertEqual((len(audits), audits[0].allowed, audits[0].reason_code),
                         (1, False, "grant_missing"))
        self.assertEqual(runner.delivery.recovery_inventory(OWNER), ())

    def test_sqlite_gate_failure_stops_child_before_any_external_operation(self):
        task_id = self.new_task()
        runner, _gate = self.executor(task_id)
        with patch.object(self.inbox._store, "write_permission_audit",
                          side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                runner.execute(task_id, "slot-a")
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)
        self.assertEqual(runner.delivery.recovery_inventory(OWNER), ())

    def test_real_worker_failure_marks_exhausted_failed_and_releases_lease(self):
        task_id = self.new_task()
        runner, gate = self.executor(task_id, command=FAIL_CODE)
        outcome = runner.execute(task_id, "slot-a")
        self.assertEqual((outcome.state, outcome.text), (TaskState.FAILED, None))
        self.assertEqual(runner.delivery.snapshot(task_id, OWNER).status, "exhausted")
        self.assertEqual([entry.allowed for entry in gate.audit_history(task_id, OWNER)],
                         [True])
        self.assertEqual([e.outcome for e in runner.provider_history(task_id)],
                         ["worker_failure"])
        next_task = self.new_task()
        self.inbox.claim_task(next_task, "worker-a", "slot-a", OWNER)

    def test_watchdog_stops_hung_model_without_false_success(self):
        task_id = self.new_task()
        runner, _gate = self.executor(task_id, command=HANG_CODE,
                                      timeout_seconds=0.12)
        outcome = runner.execute(task_id, "slot-a")
        self.assertEqual((outcome.state, outcome.text), (TaskState.TIMED_OUT, None))
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.TIMED_OUT)
        self.assertEqual(runner.delivery.recovery_inventory(OWNER), ())
        self.assertEqual([e.outcome for e in runner.provider_history(task_id)],
                         ["timed_out"])

    def test_quota_outcome_persisted_as_safe_category(self):
        task_id = self.new_task()
        command = ("import sys;sys.stdin.buffer.read();"
                   "print('{\"ok\":false,\"error\":\"quota\"}',flush=True);"
                   "raise SystemExit(2)")
        runner, _gate = self.executor(task_id, command=command)
        outcome = runner.execute(task_id, "slot-a")
        self.assertEqual(outcome.state, TaskState.FAILED)
        self.assertEqual(runner.provider_history(task_id)[0].outcome, "quota")
        self.assertEqual(runner.delivery.snapshot(task_id, OWNER).status, "exhausted")

    def test_private_v5_task_database_migrates_to_provider_audit_v7(self):
        self.inbox.close()
        with sqlite3.connect(self.db_path) as db:
            db.execute("DROP TABLE task_provider_audit")
            db.execute("PRAGMA user_version=5")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db_path),
                               workspace_root=self.root)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 14)
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master "
                                            "WHERE name='task_provider_audit'").fetchone())

    def test_untrusted_worker_output_is_rejected_not_saved_as_success(self):
        task_id = self.new_task()
        runner, _gate = self.executor(task_id, command=MALFORMED_CODE)
        with self.assertRaises(GPTTaskError):
            runner.execute(task_id, "slot-a")
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.CANCELLED)

    def test_foreign_owner_cannot_dispatch_or_see_task(self):
        task_id = self.new_task(scope=FOREIGN)
        runner, _gate = self.executor(task_id, grant=False)
        with self.assertRaises(TaskNotFound):
            runner.execute(task_id, "slot-a")
        self.assertEqual(self.inbox.state(task_id, FOREIGN), TaskState.QUEUED)

    def test_different_workspace_or_worker_cannot_steal_assignment(self):
        task_id = self.new_task()
        self.inbox.claim_task(task_id, "worker-b", "slot-a", OWNER)
        runner, _gate = self.executor(task_id)
        with self.assertRaises(GPTTaskError):
            runner.execute(task_id, "slot-a")
        self.assertIsNone(runner.delivery.snapshot(task_id, OWNER))
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.ASSIGNED)

    def test_bad_prompt_fails_before_claim_and_delivery(self):
        task_id = self.new_task(prompt="a" * 4001)
        runner, _gate = self.executor(task_id)
        with self.assertRaises(GPTTaskError):
            runner.execute(task_id, "slot-a")
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.QUEUED)
        self.assertIsNone(runner.delivery.snapshot(task_id, OWNER))

    def test_dangerous_inherited_api_key_is_not_forwarded_to_worker(self):
        task_id = self.new_task()
        runner, _gate = self.executor(task_id, command=ENV_CODE)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-paid-secret"}):
            result = runner.execute(task_id, "slot-a")
        self.assertEqual(result.text, "True")

    def test_reopen_restores_durable_success_and_no_pending_retry(self):
        task_id = self.new_task()
        runner, _gate = self.executor(task_id)
        self.assertEqual(runner.execute(task_id, "slot-a").state,
                         TaskState.SUCCEEDED)
        for executor in self.executors:
            executor.close()
        self.executors.clear()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.db_path),
                               workspace_root=self.root)
        self.assertEqual(self.inbox.state(task_id, OWNER), TaskState.SUCCEEDED)
        self.assertEqual(self.inbox.snapshot(OWNER)[0].task_id, task_id)


class PrivateGPTWorkerUnitTests(unittest.TestCase):
    def test_provider_failures_are_classified_without_error_details(self):
        for status, category in ((429, "quota"), (401, "auth"),
                                 (403, "auth"), (400, "unsupported"),
                                 (503, "unavailable")):
            with self.subTest(status=status):
                error = PlanRequestError(http_status=status, error_code=None,
                                         error_param=None, body_shape="error")
                self.assertEqual(_safe_error_category(error), category)

    def test_validation_does_not_call_provider(self):
        with patch("ai_agent_engine.gpt_task_worker.request_answer") as call:
            for bad in (b"", b"[]", b"{\"prompt\":\"x\"}",
                        b"{\"model_id\":\"gpt\",\"prompt\":\"\"}",
                        b"{" + b"x" * 20_000 + b"}"):
                with self.subTest(bad=bad[:15]), self.assertRaises(ValueError):
                    execute_request(bad)
            call.assert_not_called()

    def test_worker_uses_existing_inference_entry_not_paid_api(self):
        with patch("ai_agent_engine.gpt_task_worker.request_answer",
                   return_value="SAFE ANSWER") as call:
            response = execute_request(json.dumps({
                "model_id": MODEL, "prompt": "one approved request",
            }).encode())
        self.assertEqual(response, {"ok": True, "text": "SAFE ANSWER"})
        call.assert_called_once_with(MODEL, "one approved request")
