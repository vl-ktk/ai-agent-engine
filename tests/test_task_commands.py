"""6.7: exact-command grants and real offline subprocess tests, no network."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine import Action, Agent, ModelTurn, ScriptedModel
from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_commands import (
    CommandDenied, CommandFailed, CommandResult, MAX_OUTPUT_BYTES,
    TaskCommandRunner, _CommandSpec,
)
from ai_agent_engine.task_permissions import (
    ActionGrant, PermissionDenied, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
FOREIGN = OwnerScope("other-owner", frozenset({"project-a"}))
EXEC = ProtectedAction.EXECUTE_TOOL
WRITE = ProtectedAction.WRITE_WORKSPACE


class TaskCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-67-")
        base = Path(self.temp.name)
        root = base / "workspaces"
        root.mkdir(mode=0o700)
        self.a = root / "slot-a"
        self.b = root / "slot-b"
        self.a.mkdir(mode=0o700)
        self.b.mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(base / "tasks.sqlite3"),
                               workspace_root=WorkspaceRoot(root))
        self.task_id = self.inbox.accept(parse_submission(
            b'{"project_id":"project-a","title":"Offline command","prompt":"Run tests"}'
        ), OWNER).task_id
        self.inbox.claim_task(self.task_id, "worker-a", "slot-a", OWNER)
        subprocess.run(["git", "init", "-q", str(self.a)], check=True,
                       capture_output=True, timeout=4)
        (self.a / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.a, check=True,
                       capture_output=True, timeout=4)

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def gate(self, *actions):
        return TaskPermissionGate(self.inbox, grants=tuple(
            ActionGrant(self.task_id, "worker-a", a) for a in actions))

    def runner(self, gate, *, tests=False):
        return TaskCommandRunner(
            self.inbox, OWNER, task_id=self.task_id, worker_id="worker-a",
            gate=gate, allow_trusted_tests=tests,
        )

    def start(self):
        self.inbox.transition_state(self.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")

    def test_read_only_git_and_ram_compile_checks_are_real(self):
        gate = self.gate(EXEC)
        runner = self.runner(gate)
        (self.a / "ai_agent_engine").mkdir()
        (self.a / "ai_agent_engine" / "valid.py").write_text(
            "value = 123\n", encoding="utf-8")
        self.start()
        status = runner.run("git_status")
        self.assertIsInstance(status, CommandResult)
        self.assertEqual((status.command_id, status.exit_code), ("git_status", 0))
        self.assertIn("README.md", status.output)
        self.assertEqual(runner.run("git_diff_check").exit_code, 0)
        compile_check = runner.run("python_compile_check")
        self.assertIn("syntax_pass=1", compile_check.output)
        self.assertFalse((self.a / "ai_agent_engine" / "__pycache__").exists())
        self.assertEqual([e.action for e in gate.audit_history(self.task_id, OWNER)],
                         [EXEC, EXEC, EXEC])

    def test_agent_tool_only_accepts_allowlisted_command_id(self):
        gate = self.gate(EXEC)
        runner = self.runner(gate)
        self.start()
        permitted = Agent(model=ScriptedModel((
            ModelTurn(action=Action("run_vetted_command", {"command_id": "git_status"})),
            ModelTurn(final_text="done"),
        )), tools=runner.allowed_tools()).run("inspect")
        self.assertEqual(permitted.status, "done")
        denied = Agent(model=ScriptedModel((
            ModelTurn(action=Action("run_vetted_command", {
                "command_id": "git_status", "argv": [";touch pwn"]})),
        )), tools=runner.allowed_tools()).run("inspect")
        self.assertEqual(denied.status, "blocked")
        self.assertFalse((self.a / "pwn").exists())

    def test_git_fsmonitor_config_cannot_run_workspace_hook(self):
        hook = self.a / "fsmonitor-hook.sh"
        marker = self.a / "hook-ran"
        hook.write_text("#!/bin/sh\ntouch hook-ran\nexit 0\n", encoding="utf-8")
        hook.chmod(0o700)
        subprocess.run(["git", "config", "core.fsmonitor", str(hook)],
                       cwd=self.a, check=True, capture_output=True, timeout=4)
        runner = self.runner(self.gate(EXEC))
        self.start()
        runner.run("git_status")
        self.assertFalse(marker.exists())

    def test_unknown_commands_and_shell_injection_never_run_or_audit(self):
        gate = self.gate(EXEC, WRITE)
        runner = self.runner(gate)
        self.start()
        for value in ("sh", "bash", "git_push", "git_fetch", "git status",
                      "git_status;touch escaped", "../git_status", "python -c",
                      "python_unit_tests --start-directory /", "", None, 42):
            with self.subTest(value=value), self.assertRaises(CommandDenied):
                runner.run(value)
        self.assertEqual(gate.audit_history(self.task_id, OWNER), ())
        self.assertFalse((self.a / "escaped").exists())

    def test_default_deny_and_wrong_worker_scope(self):
        no_grants = self.gate()
        runner = self.runner(no_grants)
        self.start()
        with self.assertRaises(PermissionDenied):
            runner.run("git_status")
        self.assertEqual([e.reason_code for e in no_grants.audit_history(
            self.task_id, OWNER)], ["grant_missing"])
        with self.assertRaises(CommandDenied):
            TaskCommandRunner(self.inbox, FOREIGN, task_id=self.task_id,
                              worker_id="worker-a", gate=no_grants)
        with self.assertRaises(CommandDenied):
            TaskCommandRunner(self.inbox, OWNER, task_id=self.task_id,
                              worker_id="other-worker", gate=no_grants)

    def test_non_running_task_denied_before_process_spawn(self):
        gate = self.gate(EXEC)
        runner = self.runner(gate)
        with self.assertRaises(PermissionDenied):
            runner.run("git_status")
        self.assertEqual([e.reason_code for e in gate.audit_history(
            self.task_id, OWNER)], ["task_not_running"])

    def test_tests_need_both_trusted_opt_in_and_write_grant(self):
        (self.a / "tests").mkdir()
        (self.a / "tests" / "test_smoke.py").write_text(
            "import unittest\n"
            "class Smoke(unittest.TestCase):\n"
            "  def test_ok(self): self.assertEqual(2+2,4)\n", encoding="utf-8")
        gate = self.gate(EXEC)
        self.start()
        with self.assertRaises(CommandDenied):
            self.runner(gate, tests=False).run("python_unit_tests")
        self.assertEqual(gate.audit_history(self.task_id, OWNER), ())
        with self.assertRaises(PermissionDenied):
            self.runner(gate, tests=True).run("python_unit_tests")
        self.assertEqual([(e.action, e.allowed) for e in gate.audit_history(
            self.task_id, OWNER)], [(EXEC, True), (WRITE, False)])

    def test_vetted_test_command_runs_only_with_both_grants(self):
        (self.a / "tests").mkdir()
        (self.a / "tests" / "test_smoke.py").write_text(
            "import unittest\n"
            "class Smoke(unittest.TestCase):\n"
            "  def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
        gate = self.gate(EXEC, WRITE)
        runner = self.runner(gate, tests=True)
        self.start()
        result = runner.run("python_unit_tests")
        self.assertEqual((result.command_id, result.exit_code),
                         ("python_unit_tests", 0))
        self.assertEqual([(e.action, e.allowed) for e in gate.audit_history(
            self.task_id, OWNER)], [(EXEC, True), (WRITE, True)])
        self.assertFalse((self.a / "tests" / "__pycache__").exists())

    def test_expired_grant_refuses_execution(self):
        gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(self.task_id, "worker-a", EXEC, expires_at=1),))
        self.start()
        with self.assertRaises(PermissionDenied):
            self.runner(gate).run("git_status")
        self.assertEqual([e.reason_code for e in gate.audit_history(
            self.task_id, OWNER)], ["grant_expired"])

    def test_git_diff_check_detects_dirty_trailing_whitespace(self):
        (self.a / "README.md").write_text("bad trailing \n", encoding="utf-8")
        runner = self.runner(self.gate(EXEC))
        self.start()
        with self.assertRaises(CommandFailed):
            runner.run("git_diff_check")
        self.assertEqual((self.a / "README.md").read_text(), "bad trailing \n")

    def test_harmful_env_variables_not_passed_to_child(self):
        runner = self.runner(self.gate(EXEC))
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "do-not-send", "ANTHROPIC_API_KEY": "do-not-send",
            "CLAUDE_CODE_OAUTH_TOKEN": "do-not-send", "GIT_CONFIG_COUNT": "123",
            "PYTHONPATH": "/evil", "LD_PRELOAD": "/evil/lib.so",
        }):
            environment = runner._environment(str(self.a))
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                     "CLAUDE_CODE_OAUTH_TOKEN", "GIT_CONFIG_COUNT", "LD_PRELOAD"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PYTHONPATH"], "")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_workspace_replacement_is_denied_before_command_start(self):
        gate = self.gate(EXEC)
        runner = self.runner(gate)
        self.start()
        old = self.a.with_name("slot-a-original")
        self.a.rename(old)
        self.a.symlink_to(self.b, target_is_directory=True)
        with self.assertRaises(CommandDenied):
            runner.run("git_status")

    def test_output_limit_stops_spam_from_trusted_test_spec(self):
        runner = self.runner(self.gate(EXEC))
        self.start()
        # Only trusted test harness replaces the private allowlist, never
        # a model prompt or HTTP request.
        large = _CommandSpec((sys.executable, "-c",
                              f"print('x'*{MAX_OUTPUT_BYTES + 100})"), 2)
        with patch.object(runner, "_commands", {"synthetic": large}):
            with self.assertRaises(CommandFailed):
                runner.run("synthetic")

    def test_timeout_kills_test_process_in_own_group(self):
        runner = self.runner(self.gate(EXEC))
        self.start()
        sleeper = _CommandSpec((sys.executable, "-c",
                                "import time;time.sleep(60)"), 0.1)
        with patch.object(runner, "_commands", {"synthetic": sleeper}):
            with self.assertRaises(CommandFailed):
                runner.run("synthetic")


if __name__ == "__main__":
    unittest.main()
