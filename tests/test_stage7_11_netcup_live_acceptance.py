"""Offline guards for Netcup's one-shot owner-scoped capacity E2E."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if os.name != "posix":
    raise unittest.SkipTest("Netcup owner administration requires POSIX")

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "stage7_11_capacity_live_acceptance",
    ROOT / "ops/admin-scripts/stage7_11_capacity_live_acceptance.py",
)
ADMIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADMIN)


def receipt(status):
    return ("STAGE7_11_OWNER_E2E " + json.dumps({
        "status": status, "providers": ["gpt", "claude"],
        "cpu_slot_wait": "PASS", "provider_quota_wait": "PASS",
        "delivery_not_spent_on_wait": True, "durable_sqlite_v17": True,
        "live_provider_calls": 2 if status == "LIVE_PASS" else 0,
    }) + "\n").encode()


class NetcupStage711ReviewTests(unittest.TestCase):
    def test_exact_private_source_receipts_only(self):
        self.assertTrue(ADMIN._status(receipt("OFFLINE_PASS"), "OFFLINE_PASS"))
        self.assertTrue(ADMIN._status(receipt("LIVE_PASS"), "LIVE_PASS"))
        self.assertFalse(ADMIN._status(receipt("OFFLINE_PASS"), "LIVE_PASS"))
        corrupted = json.loads(receipt("LIVE_PASS").split(b" ", 1)[1])
        corrupted["live_provider_calls"] = 4
        self.assertFalse(ADMIN._status(("STAGE7_11_OWNER_E2E " +
            json.dumps(corrupted)).encode(), "LIVE_PASS"))
        self.assertFalse(ADMIN._status(b"fake private provider reply", "LIVE_PASS"))

    def test_owner_process_strips_inherited_credentials_and_never_uses_shell(self):
        class Account:
            pw_dir = "/private/ae-runtime"
        with patch.object(ADMIN, "_process", return_value=(True, b"")) as proc:
            ADMIN._owner_command(Account(), ["/usr/bin/true"],
                                 source=Path("/private/test-source"))
        arguments = proc.call_args.args[0]
        self.assertEqual(arguments[:6], ["/usr/sbin/runuser", "-u",
                                          "ae-runtime", "--", "/usr/bin/env", "-i"])
        self.assertIn("PYTHONPATH=/private/test-source", arguments)
        self.assertNotIn("ANTHROPIC_API_KEY", " ".join(arguments))
        self.assertFalse(proc.call_args.kwargs.get("shell", False))

    def test_refresh_failure_never_checks_or_calls_models(self):
        class Account:
            pw_dir = "/private/ae-runtime"
        with tempfile.TemporaryDirectory() as name:
            python = Path(name) / "python"
            claude = Path(name) / "claude"
            python.touch()
            claude.touch()
            with patch.object(ADMIN, "PYTHON", python), patch.object(ADMIN, "CLAUDE", claude):
                with patch.object(ADMIN, "_owner_command", return_value=(False, b"")) as command:
                    with self.assertRaisesRegex(RuntimeError, "refresh blocked"):
                        ADMIN._authorize_existing_sessions(Account())
                command.assert_called_once()
                self.assertIn("refresh_if_needed", " ".join(command.call_args.args[1]))

    def test_claimed_claude_session_rejects_paid_api_provider(self):
        class Account:
            pw_dir = "/private/ae-runtime"
        with tempfile.TemporaryDirectory() as name:
            python = Path(name) / "python"
            claude = Path(name) / "claude"
            python.touch()
            claude.touch()
            outputs = [(True, b""),
                       (True, b"Saved ChatGPT identity: [masked]\nChatGPT plan permission: GRANTED"),
                       (True, b""),
                       (True, json.dumps({"loggedIn": True, "authMethod": "api_key",
                                          "subscriptionType": "pro"}).encode())]
            with patch.object(ADMIN, "PYTHON", python), patch.object(ADMIN, "CLAUDE", claude):
                with patch.object(ADMIN, "_owner_command", side_effect=outputs):
                    with self.assertRaisesRegex(RuntimeError, "Claude subscription"):
                        ADMIN._authorize_existing_sessions(Account())

    def test_disposable_package_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "src"
            source.mkdir()
            (source / "module.py").write_text("x=1")
            (source / "unsafe").symlink_to(source / "module.py")
            with patch.object(ADMIN, "SOURCE", source):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    ADMIN._prepare_disposable_source(
                        root / "scratch", owner_uid=12345, owner_gid=12345)

    def test_root_owned_disposable_copy_is_recursively_owned_by_runtime(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "protected-source"
            source.mkdir(mode=0o700)
            (source / "__init__.py").write_text("# private fixture\n")
            nested = source / "nested"
            nested.mkdir(mode=0o700)
            (nested / "worker.py").write_text("ready=True\n")
            scratch = root / "scratch"
            scratch.mkdir()
            with patch.object(ADMIN, "SOURCE", source):
                with patch.object(ADMIN.os, "chown") as chown:
                    self.assertEqual(ADMIN._prepare_disposable_source(
                        scratch, owner_uid=12345, owner_gid=54321), scratch)
            target = scratch / "ai_agent_engine"
            expected = {target, target / "__init__.py", target / "nested",
                        target / "nested/worker.py"}
            self.assertEqual({call.args[0] for call in chown.call_args_list},
                             expected)
            self.assertEqual(chown.call_count, len(expected))
            for call in chown.call_args_list:
                self.assertEqual(call.args[1:], (12345, 54321))
                self.assertIs(call.kwargs["follow_symlinks"], False)
            self.assertTrue((target / "nested/worker.py").is_file())

    def test_disposable_copy_rejects_root_runtime_identity(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(RuntimeError, "non-root"):
                ADMIN._prepare_disposable_source(
                    Path(name), owner_uid=0, owner_gid=0)

    def test_main_refuses_ownerless_request_before_server_access(self):
        with patch.object(ADMIN.os, "geteuid", return_value=1000):
            with patch.object(ADMIN, "_baseline") as state:
                with self.assertRaisesRegex(RuntimeError, "owner-admin request"):
                    ADMIN.main()
                state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
