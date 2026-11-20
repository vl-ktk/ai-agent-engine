"""Stage 6.3 owner-only Claude adapter: all model calls are synthetic."""

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

from ai_agent_engine.claude_task_worker import (
    ClaudeWorkerError, execute_request, _safe_failure,
)
from ai_agent_engine.task_api import OwnerScope, TaskInbox, TaskNotFound, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_claude import ClaudeTaskExecutor
from ai_agent_engine.task_permissions import (
    ActionGrant, PermissionDenied, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


OWNER = OwnerScope("owner-personal", frozenset({"only-my-project"}))
OTHER = OwnerScope("other-user", frozenset({"only-my-project"}))
REPLY = (
    "import sys,json;d=json.loads(sys.stdin.buffer.read());"
    "print(json.dumps({'ok':True,'text':'claude-test:'+d['prompt']}))"
)
FAIL = ("import sys;sys.stdin.buffer.read();"
        "print('{\"ok\":false,\"error\":\"quota\"}');"
        "raise SystemExit(2)")
ENV = ("import sys,os,json;sys.stdin.buffer.read();"
       "print(json.dumps({'ok':True,'text':str(all(k not in os.environ for k in "
       "('ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','NODE_OPTIONS')))}))")


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-63-")
        self.dir = Path(self.temp.name)
        ws = self.dir / "workspaces"
        ws.mkdir(mode=0o700)
        (ws / "private").mkdir(mode=0o700)
        self.path = self.dir / "tasks.sqlite3"
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path),
                               workspace_root=WorkspaceRoot(ws))
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.inbox.close()
        self.temp.cleanup()

    def task(self, *, scope=OWNER):
        payload = json.dumps({"project_id": "only-my-project",
                              "title": "personal Claude", "prompt": "safe test"}).encode()
        return self.inbox.accept(parse_submission(payload), scope).task_id

    def runner(self, task_id, *, scope=OWNER, command=REPLY, grant=True):
        grants = ((ActionGrant(task_id, "claude-worker",
                               ProtectedAction.EXTERNAL_REQUEST),)
                  if grant else ())
        gate = TaskPermissionGate(self.inbox, grants=grants)
        runner = ClaudeTaskExecutor(
            self.inbox, scope, authorized_owner_id="owner-personal",
            worker_id="claude-worker", permission_gate=gate,
            timeout_seconds=3, worker_argv=(sys.executable, "-c", command))
        self.runners.append(runner)
        return runner, gate

    def test_real_private_child_one_ticket_audit_and_durable_success(self):
        tid = self.task()
        runner, gate = self.runner(tid)
        outcome = runner.execute(tid, "private")
        self.assertEqual(outcome.state, TaskState.SUCCEEDED)
        self.assertEqual(outcome.text, "claude-test:safe test")
        self.assertEqual(runner.delivery.snapshot(tid, OWNER).status, "completed")
        self.assertEqual([(e.provider, e.outcome) for e in runner.provider_history(tid)],
                         [("claude", "completed")])
        self.assertEqual([a.allowed for a in gate.audit_history(tid, OWNER)], [True])
        self.assertFalse(runner.execute(tid, "private").executed)
        runner.close()
        self.inbox.close()
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path),
                               workspace_root=WorkspaceRoot(self.dir / "workspaces"))
        self.assertIs(self.inbox.state(tid, OWNER), TaskState.SUCCEEDED)

    def test_24_duplicate_calls_only_launch_one_model(self):
        tid = self.task()
        runner, gate = self.runner(tid)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: runner.execute(tid, "private"),
                                    range(24)))
        self.assertEqual(sum(r.executed for r in results), 1)
        self.assertEqual(len(gate.audit_history(tid, OWNER)), 1)

    def test_no_permission_means_no_external_request(self):
        tid = self.task()
        runner, gate = self.runner(tid, grant=False)
        with self.assertRaises(PermissionDenied):
            runner.execute(tid, "private")
        self.assertEqual(self.inbox.state(tid, OWNER), TaskState.CANCELLED)
        self.assertFalse(gate.audit_history(tid, OWNER)[0].allowed)

    def test_account_owner_mismatch_rejected_before_dispatch(self):
        tid = self.task()
        gate = TaskPermissionGate(self.inbox)
        with self.assertRaises(ValueError):
            ClaudeTaskExecutor(self.inbox, OTHER,
                               authorized_owner_id="owner-personal",
                               worker_id="claude-worker", permission_gate=gate)
        runner, _ = self.runner(tid)
        with self.assertRaises(TaskNotFound):
            runner.execute(self.task(scope=OTHER), "private")

    def test_quota_failure_is_redacted_and_release_workspace(self):
        tid = self.task()
        runner, _ = self.runner(tid, command=FAIL)
        result = runner.execute(tid, "private")
        self.assertEqual(result.state, TaskState.FAILED)
        self.assertEqual(runner.provider_history(tid)[0].outcome, "quota")
        self.assertEqual(runner.delivery.snapshot(tid, OWNER).status, "exhausted")

    def test_paid_auth_and_node_preload_removed_from_child(self):
        tid = self.task()
        runner, _ = self.runner(tid, command=ENV)
        with patch.dict(os.environ, {
                "ANTHROPIC_API_KEY": "not-a-live-key",
                "CLAUDE_CODE_OAUTH_TOKEN": "not-a-real-token",
                "NODE_OPTIONS": "--require fake.js"}):
            self.assertEqual(runner.execute(tid, "private").text, "True")

    def test_v6_audit_upgrade_preserves_existing_gpt_events_and_ids(self):
        tid = self.task()
        self.inbox._store.write_provider_audit(tid, "gpt-old", "completed")
        self.inbox.close()
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE task_provider_audit RENAME TO task_provider_audit_v7")
            db.execute("""CREATE TABLE task_provider_audit (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                provider TEXT NOT NULL CHECK(provider IN ('gpt')),
                model_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN
                    ('completed','quota','auth','unsupported','unavailable',
                     'worker_failure','invalid_result','timed_out','cancelled')),
                occurred_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            db.execute("""INSERT INTO task_provider_audit
                SELECT * FROM task_provider_audit_v7""")
            db.execute("DROP TABLE task_provider_audit_v7")
            db.execute("PRAGMA user_version=6")
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.path),
                               workspace_root=WorkspaceRoot(self.dir / "workspaces"))
        previous = self.inbox._store.provider_history(tid)
        self.assertEqual((previous[0][0], previous[0][2], previous[0][3]),
                         (1, "gpt", "gpt-old"))
        self.inbox._store.write_provider_audit(tid, "default", "completed",
                                               provider="claude")
        self.assertEqual([e[2] for e in self.inbox._store.provider_history(tid)],
                         ["gpt", "claude"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 17)


class ClaudeWorkerTests(unittest.TestCase):
    def _request(self, data=None):
        return json.dumps(data or {"model_id": "default",
                                   "prompt": "One owner task"}).encode()

    @staticmethod
    def completed(stdout):
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout=json.dumps(stdout).encode(), stderr=b"")

    def test_cli_subscription_only_prompt_via_stdin_no_tools_no_api(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[-2:] == ["auth", "status"]:
                return self.completed({"authMethod": "claude.ai", "loggedIn": True,
                                       "subscriptionType": "pro", "apiProvider": "firstParty"})
            return self.completed({"type": "result", "subtype": "success",
                                   "is_error": False, "result": "AE-63-OK"})

        with patch("ai_agent_engine.claude_task_worker.shutil.which",
                   return_value="/usr/bin/claude"), patch(
                "ai_agent_engine.claude_task_worker.subprocess.run",
                side_effect=fake_run):
            self.assertEqual(execute_request(self._request())["text"], "AE-63-OK")
        self.assertEqual(len(calls), 2)
        argv, kwargs = calls[1]
        self.assertNotIn("One owner task", argv)
        self.assertEqual(kwargs["input"], b"One owner task")
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("--model", argv)

    def test_api_auth_and_other_user_method_refused_before_request(self):
        for bad in ({"authMethod": "api_key", "loggedIn": True,
                     "subscriptionType": "pro"},
                    {"authMethod": "claude.ai", "loggedIn": True,
                     "subscriptionType": "team"},
                    {"authMethod": "claude.ai", "loggedIn": True,
                     "subscriptionType": "pro", "apiProvider": "aws"}):
            with self.subTest(bad=bad), patch(
                    "ai_agent_engine.claude_task_worker.shutil.which",
                    return_value="/usr/bin/claude"), patch(
                    "ai_agent_engine.claude_task_worker.subprocess.run",
                    return_value=self.completed(bad)) as run:
                with self.assertRaises(ClaudeWorkerError) as exc:
                    execute_request(self._request())
                self.assertEqual(exc.exception.category, "auth")
                run.assert_called_once()

    def test_bad_payload_never_calls_cli(self):
        with patch("ai_agent_engine.claude_task_worker.shutil.which") as find:
            for bad in (b"", b"[]", b"{}", b"{" + b"x" * 20_000 + b"}",
                        self._request({"model_id": "--api", "prompt": "x"})):
                with self.subTest(bad=bad[:14]), self.assertRaises(ClaudeWorkerError):
                    execute_request(bad)
            find.assert_not_called()

    def test_status_429_is_quota_not_error_text(self):
        self.assertEqual(_safe_failure({"api_error_status": 429,
                                        "result": "secret"}), "quota")


if __name__ == "__main__":
    unittest.main()
