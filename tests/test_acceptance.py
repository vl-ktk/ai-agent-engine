"""All Claude Code tests are synthetic: never invoke a live CLI or paid API."""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from ai_agent_engine import acceptance


class DummyStore:
    def __init__(self, session):
        self.session = session

    def load_session(self):
        return self.session


def grant():
    return {
        "sharing_enabled": True,
        "scopes": [acceptance.DIRECT_SCOPE, "resource.invoke"],
    }


def cli_reply(payload, *, returncode=0, stderr=b""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode,
        stdout=json.dumps(payload).encode("utf-8"),
        stderr=stderr,
    )


class AcceptanceTests(unittest.TestCase):
    def test_gpt_preflight_requires_local_grant(self):
        acceptance.check_gpt(DummyStore(grant()))
        for bad in (None, {}, {"sharing_enabled": False, "scopes": []},
                    {"sharing_enabled": True, "scopes": ["resource.invoke"]}):
            with self.subTest(bad=bad):
                with self.assertRaises(acceptance.AcceptanceError):
                    acceptance.check_gpt(DummyStore(bad))

    def test_no_api_environment_override(self):
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_MODEL", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            with self.subTest(key=key):
                with self.assertRaises(acceptance.AcceptanceError):
                    acceptance._validate_environment({key: "not-a-real-secret"})
        acceptance._validate_environment({"PATH": "/usr/bin", "LANG": "en_US.UTF-8"})

    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run")
    def test_claude_subscription_status_only(self, runner, which):
        runner.return_value = cli_reply({"authMethod": "claude.ai"})
        acceptance.check_claude(env={})
        runner.assert_called_once()
        options = runner.call_args
        self.assertEqual(options.args[0], ["/usr/local/bin/claude", "auth", "status"])
        self.assertEqual(options.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(options.kwargs["check"])

    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run")
    def test_rejects_non_subscription_methods(self, runner, which):
        for method in ("api_key", "oauth_token", "third_party",
                       "api_key_helper", "none", None):
            with self.subTest(method=method):
                runner.return_value = cli_reply({"authMethod": method})
                with self.assertRaises(acceptance.AcceptanceError):
                    acceptance.check_claude(env={})

    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run")
    def test_probe_text_only_one_turn_no_mcp(self, runner, which):
        runner.side_effect = [
            cli_reply({"authMethod": "claude.ai"}),
            cli_reply({
                "type": "result", "subtype": "success",
                "is_error": False, "result": "Hello, world!\n",
            }),
        ]
        acceptance.probe_claude(env={})
        self.assertEqual(runner.call_count, 2)
        cmd = runner.call_args.args[0]
        self.assertIn("--bare", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--max-turns", cmd)
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "1")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "*")
        self.assertIn("--no-chrome", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertIsNotNone(runner.call_args.kwargs["cwd"])

    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run")
    def test_probe_fails_closed_on_unexpected_result(self, runner, which):
        for payload in (
            {"type": "result", "subtype": "error", "is_error": True, "result": "Hello, world!"},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Almost"},
            {"type": "event", "subtype": "success", "is_error": False, "result": "Hello, world!"},
            {},
        ):
            with self.subTest(payload=payload):
                runner.side_effect = [
                    cli_reply({"authMethod": "claude.ai"}),
                    cli_reply(payload),
                ]
                with self.assertRaises(acceptance.AcceptanceError):
                    acceptance.probe_claude(env={})

    @patch.object(acceptance, "check_gpt")
    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run")
    def test_provider_output_never_printed_on_errors(self, runner, which, gpt):
        runner.return_value = cli_reply(
            {"secret": "NO_PRINT_TOKEN"}, returncode=1,
            stderr=b"hidden provider response NO_PRINT_TOKEN",
        )
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(acceptance.main(["check"]), 1)
        self.assertNotIn("NO_PRINT_TOKEN", output.getvalue())

    @patch.object(acceptance.shutil, "which", return_value=None)
    def test_missing_claude_cli_is_blocker(self, which):
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.check_claude(env={})

    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run")
    def test_malformed_cli_json_rejected(self, runner, which):
        runner.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=b"{broken json",
            stderr=b"do not print",
        )
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.check_claude(env={})

    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run")
    def test_oversized_cli_json_rejected(self, runner, which):
        runner.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=b"x" * (acceptance.MAX_CLAUDE_JSON_BYTES + 1),
            stderr=b"",
        )
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.check_claude(env={})

    @patch.object(acceptance.shutil, "which", return_value="/usr/local/bin/claude")
    @patch.object(acceptance.subprocess, "run", side_effect=subprocess.TimeoutExpired(["claude"], 30))
    def test_timeout_does_not_retry(self, runner, which):
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.check_claude(env={})
        runner.assert_called_once()

    @patch.object(acceptance, "probe_claude")
    @patch.object(acceptance, "run_one_click")
    @patch.object(acceptance, "check_claude")
    @patch.object(acceptance, "check_gpt")
    @patch.object(acceptance, "CredentialStore")
    def test_live_preflights_before_provider_call(self, store, gpt, claude, run, probe):
        run.return_value = ("gpt-test", object(), True, None)
        with redirect_stdout(io.StringIO()):
            acceptance.run_live()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(probe.call_count, 1)
        gpt.assert_called_once()
        claude.assert_called_once()
        run.assert_called_once()
        probe.assert_called_once()

    @patch.object(acceptance, "probe_claude")
    @patch.object(acceptance, "run_one_click")
    @patch.object(acceptance, "check_claude", side_effect=acceptance.AcceptanceError("not signed in"))
    @patch.object(acceptance, "check_gpt")
    @patch.object(acceptance, "CredentialStore")
    def test_auth_failure_prevents_live_requests(self, store, gpt, claude, run, probe):
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.run_live()
        run.assert_not_called()
        probe.assert_not_called()

    @patch.object(acceptance, "probe_claude")
    @patch.object(acceptance, "run_one_click")
    @patch.object(acceptance, "check_claude")
    @patch.object(acceptance, "check_gpt")
    @patch.object(acceptance, "CredentialStore")
    def test_gpt_failure_prevents_claude_request(self, store, gpt, claude, run, probe):
        run.return_value = ("gpt-test", object(), False, "bounded failure")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.run_live()
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
