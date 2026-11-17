"""Offline native tool-call adapter tests. No real account, paid API, or network."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.parse import parse_qs

from ai_agent_engine import plan_agent
from ai_agent_engine.inference import InferenceError
from ai_agent_engine.siwc import CredentialStore


class FakeTransport:
    def __init__(self, *replies: bytes):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.replies:
            raise AssertionError("Unexpected provider request")
        return BytesIO(self.replies.pop(0))


def catalog():
    return json.dumps({"models": [{
        "slug": "gpt-testing", "display_name": "GPT", "visibility": "list"
    }]}).encode("utf-8")


def completed(*items):
    payload = json.dumps({
        "type": "response.completed",
        "response": {"status": "completed", "output": list(items)},
    }, separators=(",", ":")).encode("utf-8")
    return b"data: " + payload + b"\n\n" + b"data: [DONE]\n\n"


def tool_call(name, args, call_id):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(args),
    }


WRITE = tool_call("sandbox.write_new_file", {
    "path": plan_agent.FILE_NAME,
    "content": plan_agent.FILE_CONTENT,
}, "call_write_01")
READ = tool_call("sandbox.read_file", {
    "path": plan_agent.FILE_NAME,
}, "call_read_01")
FINISH = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "The file was created and verified."}],
}


class AgentToolsTests(unittest.TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = CredentialStore(Path(self.scratch.name) / "private")
        self.store.save_session({
            "subject": "owner", "client_id": "oaiapp_test",
            "access_token": "private-session-access",
            "refresh_token": "private-session-refresh",
            "sharing_enabled": True,
            "scopes": ["openid", "resource.invoke", "chatgpt.tokens.use.direct"],
            "expires_at": time.time() + 3600,
        })

    def test_agent_calls_write_and_read_and_verifies_file(self):
        transport = FakeTransport(
            catalog(), completed(WRITE), completed(READ), completed(FINISH)
        )
        slug, result, passed, last_error = plan_agent.run_one_click(
            self.store, opener=transport
        )
        self.assertEqual(slug, "gpt-testing")
        self.assertEqual(result.status, "done")
        self.assertTrue(passed)
        self.assertIsNone(last_error)
        self.assertEqual(len(transport.requests), 4)
        requests = [json.loads(req.data) for req, _ in transport.requests[1:]]
        for body in requests:
            self.assertIs(body["store"], False)
            self.assertIs(body["stream"], True)
            self.assertNotIn("max_output_tokens", body)
            self.assertNotIn("previous_response_id", body)
            self.assertEqual(body["tools"][0]["type"], "namespace")
            self.assertEqual(body["tools"][0]["name"], "sandbox")
            self.assertEqual(
                [t["name"] for t in body["tools"][0]["tools"]],
                ["write_new_file", "read_file"],
            )
        self.assertEqual(
            requests[1]["input"][-1],
            {"type": "function_call_output", "call_id": "call_write_01",
             "output": "Created gpt-agent-test.txt (26 bytes)"},
        )
        self.assertEqual(
            requests[2]["input"][-1],
            {"type": "function_call_output", "call_id": "call_read_01",
             "output": plan_agent.FILE_CONTENT},
        )
        self.assertNotIn("private-session-access", json.dumps(requests))
        self.assertEqual(
            transport.requests[1][0].get_header("Authorization"),
            "Bearer private-session-access",
        )

    def test_final_claim_without_actual_tool_calls_is_not_pass(self):
        transport = FakeTransport(catalog(), completed(FINISH))
        _, result, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertEqual(result.status, "done")
        self.assertFalse(passed)
        self.assertIsNone(err)

    def test_unauthorized_tool_fails_without_execution(self):
        unsafe = tool_call("sandbox.run_shell", {"command": "whoami"}, "call_unsafe")
        transport = FakeTransport(catalog(), completed(unsafe))
        _, result, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertEqual(result.status, "failed")
        self.assertFalse(passed)
        self.assertIn("unauthorized", err)
        self.assertEqual(len(transport.requests), 2)

    def test_multiple_tools_in_one_response_are_queued_without_extra_request(self):
        transport = FakeTransport(catalog(), completed(WRITE, READ), completed(FINISH))
        _, report, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertTrue(passed, plan_agent.describe(report))
        self.assertIsNone(err)
        self.assertEqual(report.requests_sent, 2)
        last = json.loads(transport.requests[-1][0].data)["input"]
        outputs = [i for i in last if i.get("type") == "function_call_output"]
        self.assertEqual([o["call_id"] for o in outputs], ["call_write_01", "call_read_01"])

    def test_unfinished_stream_fails_and_does_not_write(self):
        transport = FakeTransport(catalog(), b'data: {"type":"response.created"}\n\n')
        _, result, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertFalse(passed)
        self.assertEqual(result.status, "failed")
        self.assertIn("response.completed", err)

    def test_malformed_tool_schema_fails_without_side_effect(self):
        bad = tool_call("sandbox.write_new_file", {
            "path": plan_agent.FILE_NAME,
            "content": plan_agent.FILE_CONTENT,
            "unexpected": "x",
        }, "call_bad")
        transport = FakeTransport(catalog(), completed(bad))
        _, result, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertEqual(result.status, "failed")
        self.assertFalse(passed)
        self.assertIn("arguments", err)

    def test_quota_failure_in_tool_stream_aborts_without_file_side_effect(self):
        quota_stream = (
            b'data: {"type":"response.failed","response":{"error":'
            b'{"code":"subscription_sharing_usage_limit_exceeded",'
            b'"message":"private account details"}}}\n\n'
        )
        transport = FakeTransport(catalog(), quota_stream)
        slug, result, passed, error = plan_agent.run_one_click(
            self.store, opener=transport,
        )
        self.assertEqual(slug, "gpt-testing")
        self.assertEqual(result.status, "failed")
        self.assertFalse(passed)
        self.assertIn("Pause requests", error)
        self.assertNotIn("private account details", error)
        self.assertEqual(len(transport.requests), 2)

    def test_inconsistent_completed_event_status_is_not_accepted(self):
        bad = b'data: {"type":"response.completed","response":{"status":"failed","output":[]}}\n\n'
        transport = FakeTransport(catalog(), bad)
        _, result, passed, error = plan_agent.run_one_click(
            self.store, opener=transport,
        )
        self.assertEqual(result.status, "failed")
        self.assertFalse(passed)
        self.assertIn("valid output", error)

    def test_stream_reports_no_success_without_completed_event(self):
        with self.assertRaises(InferenceError):
            plan_agent._stream_output(BytesIO(
                b'data: {"type":"response.output_item.done","item":{}}\n\n'
            ))

    def test_only_known_function_schemas_can_be_exposed(self):
        with self.assertRaises(InferenceError):
            plan_agent._tool_names(("run_shell",))


    def test_trailing_period_in_content_is_reported_as_mismatch(self):
        wrong = tool_call("sandbox.write_new_file", {
            "path": plan_agent.FILE_NAME, "content": plan_agent.FILE_CONTENT + ".",
        }, "call_w")
        transport = FakeTransport(catalog(), completed(wrong), completed(READ), completed(FINISH))
        _, report, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertFalse(passed)
        self.assertIsNone(err)
        self.assertTrue(report.write_executed)
        self.assertFalse(report.write_content_ok)
        self.assertTrue(report.file_exists)
        self.assertFalse(report.file_content_ok)
        self.assertFalse(report.read_output_ok)
        self.assertEqual(report.status, "done")

    def test_tool_rejection_reason_is_recorded_safely(self):
        dotted = tool_call("write_new_file", {
            "path": "./" + plan_agent.FILE_NAME, "content": plan_agent.FILE_CONTENT,
        }, "call_dot")
        transport = FakeTransport(catalog(), completed(dotted))
        _, report, passed, _ = plan_agent.run_one_click(self.store, opener=transport)
        self.assertFalse(passed)
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.tool_errors, ["write_new_file: path escapes sandbox"])
        self.assertFalse(report.write_path_ok)
        self.assertIn("tool_error:write_new_file", report.audit)

    def test_no_tool_call_is_reported(self):
        transport = FakeTransport(catalog(), completed(FINISH))
        _, report, passed, _ = plan_agent.run_one_click(self.store, opener=transport)
        self.assertFalse(passed)
        self.assertEqual(report.requested_tools, [])
        self.assertFalse(report.write_executed)
        self.assertTrue(report.final_answer)

    def test_replay_drops_reasoning_and_server_ids(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
        write = dict(WRITE, id="fc_1", status="completed")
        transport = FakeTransport(catalog(), completed(reasoning, write),
                                  completed(READ), completed(FINISH))
        _, report, passed, _ = plan_agent.run_one_click(self.store, opener=transport)
        self.assertTrue(passed)
        second = json.loads(transport.requests[2][0].data)["input"]
        self.assertFalse(any(i.get("type") == "reasoning" for i in second))
        self.assertFalse(any("id" in i or "status" in i for i in second))

    def test_request_cap_stops_looping_model(self):
        reads = [completed(tool_call("read_file", {"path": "missing.txt"}, "c%d" % n))
                 for n in range(10)]
        missing_ok = tool_call("write_new_file", {"path": "missing.txt", "content": "x"}, "c_w")
        transport = FakeTransport(catalog(), completed(missing_ok), *reads)
        _, report, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertFalse(passed)
        self.assertLessEqual(report.requests_sent, plan_agent.MAX_TOOL_REQUESTS)
        self.assertLessEqual(len(transport.requests) - 1, plan_agent.MAX_TOOL_REQUESTS)

    def test_unexpected_exception_is_reported_by_class_only(self):
        import http.client

        class Broken:
            def __iter__(self):
                raise http.client.IncompleteRead(b"secret-partial")
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        calls = [catalog()]
        def opener(request, timeout):
            return BytesIO(calls.pop(0)) if calls else Broken()
        _, report, passed, err = plan_agent.run_one_click(self.store, opener=opener)
        self.assertFalse(passed)
        self.assertIn("interrupted", err)
        self.assertNotIn("secret-partial", err)

        calls2 = [catalog()]
        def opener2(request, timeout):
            if calls2:
                return BytesIO(calls2.pop(0))
            raise KeyError("private-session-access")
        _, report, passed, err = plan_agent.run_one_click(self.store, opener=opener2)
        self.assertEqual(err, "unexpected KeyError (details withheld)")
        self.assertNotIn("private-session-access", " ".join(plan_agent.describe(report)))

    def test_message_without_content_list_is_safe(self):
        bad = {"type": "message", "role": "assistant", "content": None}
        transport = FakeTransport(catalog(), completed(bad))
        _, report, passed, err = plan_agent.run_one_click(self.store, opener=transport)
        self.assertFalse(passed)
        self.assertIn("final agent answer", err)

    def test_prompt_states_exact_content_unambiguously(self):
        self.assertIn(json.dumps(plan_agent.FILE_CONTENT), plan_agent.TASK_PROMPT)
        self.assertIn("no trailing period", plan_agent.TASK_PROMPT)


if __name__ == "__main__":
    unittest.main()
