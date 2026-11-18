"""Stage 6.8: real Linux filesystem/socket isolation and credential redaction."""

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine.task_api import OwnerScope, TaskInbox, parse_submission
from ai_agent_engine.task_assignment import WorkspaceRoot
from ai_agent_engine.task_commands import (
    CommandDenied, CommandFailed, TaskCommandRunner, _CommandSpec,
)
from ai_agent_engine.task_isolation import (
    IsolationUnavailable, OfflineTaskSandbox, REDACTED,
    provider_worker_environment, redact_text, sensitive_environment_values,
)
from ai_agent_engine.task_permissions import (
    ActionGrant, ProtectedAction, TaskPermissionGate,
)
from ai_agent_engine.task_state import TaskState
from ai_agent_engine.task_storage import SQLiteTaskStore


OWNER = OwnerScope("owner-a", frozenset({"project-a"}))
EXEC = ProtectedAction.EXECUTE_TOOL
WRITE = ProtectedAction.WRITE_WORKSPACE


class OfflineIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-68-")
        self.base = Path(self.temp.name)
        root = self.base / "workspaces"
        root.mkdir(mode=0o700)
        self.workspace = root / "slot-a"
        self.workspace.mkdir(mode=0o700)
        self.inbox = TaskInbox(store=SQLiteTaskStore(self.base / "tasks.sqlite3"),
                               workspace_root=WorkspaceRoot(root))
        self.task_id = self.inbox.accept(parse_submission(
            b'{"project_id":"project-a","title":"Offline sandbox","prompt":"Check safety"}'
        ), OWNER).task_id
        self.inbox.claim_task(self.task_id, "worker-a", "slot-a", OWNER)
        self.inbox.transition_state(self.task_id, TaskState.RUNNING,
                                    OWNER, actor_id="worker-a")
        self.gate = TaskPermissionGate(self.inbox, grants=(
            ActionGrant(self.task_id, "worker-a", EXEC),
            ActionGrant(self.task_id, "worker-a", WRITE),
        ))

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def runner(self, *, writable=False):
        return TaskCommandRunner(self.inbox, OWNER, task_id=self.task_id,
                                 worker_id="worker-a", gate=self.gate,
                                 allow_trusted_tests=writable)

    def sandbox_script(self, source: str, *, writable=False,
                       runner=None) -> str:
        selected = runner or self.runner(writable=writable)
        spec = _CommandSpec(("/usr/bin/python3", "-I", "-B", "-c", source),
                            5, requires_test_opt_in=writable)
        # Injection is solely by the trusted test harness; production
        # model JSON cannot change TaskCommandRunner's immutable allowlist.
        with patch.object(selected, "_commands", {"synthetic": spec}):
            return selected.run("synthetic").output

    def test_readonly_sandbox_denies_workspace_and_host_writes(self):
        secret_file = self.base / "outside-only.txt"
        secret_file.write_text("private fixture", encoding="utf-8")
        source = (
            "from pathlib import Path\n"
            f"print('outside='+str(Path({str(secret_file)!r}).exists()))\n"
            "print('etc='+str(Path('/etc/passwd').exists()))\n"
            "print('usrlocal='+str(Path('/usr/local').exists()))\n"
            "try: Path('/workspace/forbidden.txt').write_text('bad')\n"
            "except OSError: print('workspace_write=blocked')\n"
            "try: Path('/tmp/can-write').write_text('ephemeral')\n"
            "except OSError: print('tmp_write=blocked')\n"
            "print('cwd='+str(Path.cwd()))\n"
        )
        result = self.sandbox_script(source)
        self.assertIn("outside=False", result)
        self.assertIn("etc=False", result)
        self.assertIn("usrlocal=False", result)
        self.assertIn("workspace_write=blocked", result)
        self.assertIn("cwd=/workspace", result)
        self.assertFalse((self.workspace / "forbidden.txt").exists())
        self.assertEqual(secret_file.read_text(), "private fixture")

    def test_kernel_filter_denies_real_network_socket(self):
        result = self.sandbox_script(
            "import socket,errno\n"
            "try: socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
            "except OSError as e: print('socket_denied='+str(e.errno==errno.EPERM))\n")
        self.assertIn("socket_denied=True", result)

    def test_writable_test_process_can_write_workspace_but_not_host(self):
        outside = self.base / "host"
        outside.write_text("unchanged", encoding="utf-8")
        result = self.sandbox_script(
            "from pathlib import Path\n"
            "Path('/workspace/allowed.txt').write_text('ok')\n"
            f"print('outside_visible='+str(Path({str(outside)!r}).exists()))\n"
            "print('written='+Path('/workspace/allowed.txt').read_text())\n",
            writable=True)
        self.assertIn("outside_visible=False", result)
        self.assertIn("written=ok", result)
        self.assertEqual((self.workspace / "allowed.txt").read_text(), "ok")
        self.assertEqual(outside.read_text(), "unchanged")

    def test_offline_process_cannot_inherit_host_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-fixture-value-123",
                                     "CLAUDE_CODE_OAUTH_TOKEN": "hidden-auth-12345",
                                     "LD_PRELOAD": "/outside/libbad.so",
                                     "PYTHONPATH": "/outside/source"}):
            runner = self.runner()
            result = self.sandbox_script(
                "import os\n"
                "print('api='+str('OPENAI_API_KEY' in os.environ))\n"
                "print('oauth='+str('CLAUDE_CODE_OAUTH_TOKEN' in os.environ))\n"
                "print('ld='+str('LD_PRELOAD' in os.environ))\n"
                "print('pythonpath='+repr(os.environ.get('PYTHONPATH')))\n",
                runner=runner)
        self.assertIn("api=False", result)
        self.assertIn("oauth=False", result)
        self.assertIn("ld=False", result)
        self.assertIn("pythonpath=''", result)

    def test_literal_host_secret_in_child_stdout_is_redacted(self):
        value = "fixture-access-very-secret-12345"
        with patch.dict(os.environ, {"SERVICE_ACCESS_TOKEN": value}):
            runner = self.runner()
        output = self.sandbox_script(f"print({value!r})", runner=runner)
        self.assertEqual(output.strip(), REDACTED)
        self.assertNotIn(value, output)

    def test_high_confidence_token_shapes_are_redacted_even_without_env(self):
        token = "sk-" + "x" * 32
        result = self.sandbox_script(f"print({token!r})")
        self.assertEqual(result.strip(), REDACTED)

    def test_no_fallback_without_bubblewrap(self):
        runner = self.runner()
        with patch.object(OfflineTaskSandbox, "_bwrap_path",
                          side_effect=IsolationUnavailable("missing")):
            with self.assertRaisesRegex(CommandFailed, "sandbox unavailable"):
                runner.run("python_compile_check")

    def test_widened_workspace_permissions_are_rejected_at_run_time(self):
        runner = self.runner()
        self.workspace.chmod(0o755)
        try:
            with self.assertRaises(CommandDenied):
                runner.run("python_compile_check")
        finally:
            self.workspace.chmod(0o700)

    def test_no_fallback_without_kernel_seccomp(self):
        runner = self.runner()
        with patch("ai_agent_engine.task_isolation._network_and_escape_filter_fd",
                   side_effect=IsolationUnavailable("missing")):
            with self.assertRaisesRegex(CommandFailed, "sandbox unavailable"):
                runner.run("python_compile_check")

    def test_redaction_helpers_mask_assignments_bearer_and_literal(self):
        token = "fixture-private-987654321"
        output = redact_text(
            f"password={token}\nAuthorization: Bearer abcdefghijk1234567\n"
            f"random {token}", secrets=(token,))
        self.assertNotIn(token, output)
        self.assertNotIn("abcdefghijk1234567", output)
        self.assertIn("password=" + REDACTED, output)

    def test_sensitivity_requires_name_and_substantive_value(self):
        result = sensitive_environment_values({
            "SESSION_TOKEN": "lengthy-token-123", "SIMPLE": "not-secret",
            "PASSWORD": "tiny", "SERVICE_API_KEY": "key-data-12345",
        })
        self.assertEqual(set(result), {"lengthy-token-123", "key-data-12345"})

    def test_provider_environment_does_not_inherit_misc_credentials(self):
        result = provider_worker_environment({
            "HOME": "/home/owner", "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8", "OPENAI_API_KEY": "token-private-12345",
            "ANTHROPIC_API_KEY": "secret-private-12345",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret-private-67890",
            "LD_PRELOAD": "/outside/evil.so", "PYTHONPATH": "/outside",
            "GITHUB_TOKEN": "secret-private-9876",
        }, "/trusted/source")
        self.assertEqual(result["HOME"], "/home/owner")
        self.assertEqual(result["PYTHONPATH"], "/trusted/source")
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                     "LD_PRELOAD", "GITHUB_TOKEN"):
            self.assertNotIn(name, result)


if __name__ == "__main__":
    unittest.main()
