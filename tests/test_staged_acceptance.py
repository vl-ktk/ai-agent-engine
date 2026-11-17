"""Staged combined acceptance, fully synthetic: no live CLI, account or network."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from ai_agent_engine import acceptance, plan_agent
from ai_agent_engine.siwc import CredentialStore

from test_plan_agent import FINISH, READ, WRITE, FakeTransport, catalog, completed

SECRET_ACCESS = "private-session-access-XYZ"
SECRET_REFRESH = "private-session-refresh-XYZ"
STATUS = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
          "subscriptionType": "pro", "email": "PRIVATE@example.invalid"}
CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False,
             "result": "Hello, world!"}


def text_reply(text="Hello, world!"):
    delta = json.dumps({"type": "response.output_text.delta", "delta": text})
    done = json.dumps({"type": "response.completed",
                       "response": {"status": "completed", "output": []}})
    return f"data: {delta}\n\ndata: {done}\n\n".encode()


def cli(payload, code=0):
    return subprocess.CompletedProcess(["claude"], code, json.dumps(payload).encode(), b"PRIVATE")


class StagedAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = CredentialStore(Path(self.scratch.name) / "private")
        self.store.save_session({
            "subject": "owner", "client_id": "oaiapp_test",
            "access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH,
            "sharing_enabled": True,
            "scopes": ["openid", "resource.invoke", "chatgpt.tokens.use.direct"],
            "expires_at": time.time() + 3600,
        })
        which = patch.object(acceptance.shutil, "which", return_value="/usr/bin/claude")
        which.start()
        self.addCleanup(which.stop)

    def run_live(self, transport, claude_replies):
        out = io.StringIO()
        with patch.object(acceptance.subprocess, "run", side_effect=claude_replies) as runner, \
                redirect_stdout(out):
            report = acceptance.run_live(store=self.store, env={"PATH": "/usr/bin"},
                                         opener=transport)
        self.runner = runner
        return report, out.getvalue()

    def statuses(self, report):
        return {s.key: s.status for s in report.stages}

    def assert_no_secrets(self, *texts):
        for text in texts:
            for secret in (SECRET_ACCESS, SECRET_REFRESH, "PRIVATE"):
                self.assertNotIn(secret, text)

    def test_full_pass_reports_every_stage(self):
        transport = FakeTransport(catalog(), catalog(), text_reply(),
                                  completed(WRITE), completed(READ), completed(FINISH))
        report, out = self.run_live(transport, [cli(STATUS), cli(CLAUDE_OK)])
        self.assertTrue(report.passed, out)
        self.assertEqual([s.key for s in report.stages],
                         [k for k, _ in acceptance.GPT_STAGES + acceptance.CLAUDE_STAGES])
        self.assertEqual(set(self.statuses(report).values()), {"PASS"})
        posts = [r for r, _ in transport.requests if r.get_method() == "POST"]
        self.assertEqual(len(posts), 4)  # 1 reply + 3 tool-cycle requests
        self.assertEqual(self.runner.call_count, 2)
        self.assert_no_secrets(out, json.dumps(report.as_dict()))

    def test_gpt_quota_skips_tools_but_claude_still_runs(self):
        quota = (b'data: {"type":"response.failed","response":{"error":'
                 b'{"code":"subscription_sharing_usage_limit_exceeded","message":"PRIVATE"}}}\n\n')
        transport = FakeTransport(catalog(), catalog(), quota)
        report, out = self.run_live(transport, [cli(STATUS), cli(CLAUDE_OK)])
        st = self.statuses(report)
        self.assertEqual(st["gpt.reply"], "FAIL")
        for key, _ in acceptance.GPT_TOOL_STAGES:
            self.assertEqual(st[key], "SKIPPED")
        for key, _ in acceptance.CLAUDE_STAGES:
            self.assertEqual(st[key], "PASS")
        self.assertFalse(report.passed)
        self.assertIn("usage limit", out)
        self.assertEqual(len(transport.replies), 0)  # no retry, nothing extra
        self.assert_no_secrets(out)

    def test_missing_gpt_sign_in_sends_no_gpt_request(self):
        empty = CredentialStore(Path(self.scratch.name) / "empty")
        transport = FakeTransport()
        out = io.StringIO()
        with patch.object(acceptance.subprocess, "run", side_effect=[cli(STATUS), cli(CLAUDE_OK)]), \
                redirect_stdout(out):
            report = acceptance.run_live(store=empty, env={"PATH": "/usr/bin"})
        st = self.statuses(report)
        self.assertEqual(st["gpt.auth"], "FAIL")
        self.assertTrue(all(st[k] == "SKIPPED" for k, _ in acceptance.GPT_STAGES[1:]))
        self.assertEqual(st["claude.completed"], "PASS")
        self.assertEqual(transport.requests, [])

    def test_gpt_without_tool_calls_fails_with_reason(self):
        transport = FakeTransport(catalog(), catalog(), text_reply(), completed(FINISH))
        report, out = self.run_live(transport, [cli(STATUS), cli(CLAUDE_OK)])
        st = self.statuses(report)
        self.assertEqual(st["gpt.tool_write"], "FAIL")
        self.assertEqual(st["gpt.file_created"], "SKIPPED")
        self.assertEqual(st["gpt.tool_read"], "SKIPPED")
        self.assertIn("did not request write_new_file", out)
        self.assertFalse(report.passed)

    def test_claude_not_signed_in_skips_request(self):
        transport = FakeTransport(catalog(), catalog(), text_reply(),
                                  completed(WRITE), completed(READ), completed(FINISH))
        report, out = self.run_live(transport, [cli({"loggedIn": False, "authMethod": "none"})])
        st = self.statuses(report)
        self.assertEqual(st["claude.auth"], "FAIL")
        for key in ("claude.subscription", "claude.request", "claude.completed"):
            self.assertEqual(st[key], "SKIPPED")
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(st["gpt.loop_done"], "PASS")

    def test_claude_request_error_is_reported_once(self):
        transport = FakeTransport(catalog(), catalog(), text_reply(),
                                  completed(WRITE), completed(READ), completed(FINISH))
        err = {"type": "result", "subtype": "error_during_execution", "is_error": True,
               "api_error_status": 429, "result": "PRIVATE"}
        report, out = self.run_live(transport, [cli(STATUS), cli(err, 1)])
        st = self.statuses(report)
        self.assertEqual(st["claude.request"], "FAIL")
        self.assertEqual(st["claude.completed"], "SKIPPED")
        self.assertIn("api_error_status=429", out)
        self.assertEqual(self.runner.call_count, 2)
        self.assert_no_secrets(out)

    def test_main_writes_redacted_json_and_exit_code(self):
        report = acceptance.Report()
        with redirect_stdout(io.StringIO()):
            report.add("gpt.auth", "GPT: ChatGPT sign-in", "PASS")
            report.add("claude.auth", "Claude", "FAIL", "x")
        path = Path(self.scratch.name) / "report.json"
        out = io.StringIO()
        with patch.object(acceptance, "run_live", return_value=report), redirect_stdout(out):
            code = acceptance.main(["run-live", "--report-json", str(path)])
        self.assertEqual(code, 1)
        data = json.loads(path.read_text())
        self.assertFalse(data["passed"])
        self.assertIn("1 passed, 1 failed, 0 skipped", out.getvalue())

    def test_unwritable_report_path_keeps_live_result(self):
        report = acceptance.Report()
        with redirect_stdout(io.StringIO()):
            report.add("gpt.auth", "GPT: ChatGPT sign-in", "PASS")
        missing = Path(self.scratch.name) / "no-such-dir" / "report.json"
        out, err = io.StringIO(), io.StringIO()
        with patch.object(acceptance, "run_live", return_value=report), \
                redirect_stdout(out), redirect_stderr(err):
            code = acceptance.main(["run-live", "--report-json", str(missing)])
        self.assertEqual(code, 0)
        self.assertIn("Result: PASS", out.getvalue())
        self.assertIn("not saved (FileNotFoundError)", err.getvalue())

    def test_unexpected_exception_is_contained(self):
        with patch.object(acceptance, "check_claude", side_effect=RuntimeError("PRIVATE")), \
                patch.object(acceptance, "run_gpt_stages"), redirect_stdout(io.StringIO()) as out:
            report = acceptance.run_live(store=self.store, env={})
        self.assertEqual(self.statuses(report)["claude.auth"], "FAIL")
        self.assertIn("unexpected RuntimeError", out.getvalue())
        self.assert_no_secrets(out.getvalue())

    def test_check_command_sends_no_model_requests(self):
        with patch.object(acceptance.subprocess, "run", side_effect=[cli(STATUS)]) as runner, \
                patch.object(acceptance, "CredentialStore", return_value=self.store), \
                patch.object(acceptance, "request_answer") as ask, \
                patch.object(acceptance, "run_tool_cycle") as cycle, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(acceptance.main(["check"]), 0)
        ask.assert_not_called()
        cycle.assert_not_called()
        self.assertEqual(runner.call_args.args[0][1:], ["auth", "status"])


if __name__ == "__main__":
    unittest.main()
