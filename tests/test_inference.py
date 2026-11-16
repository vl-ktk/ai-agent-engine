"""Offline model-probe tests: no OAuth secrets or real API requests."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.error import HTTPError

from ai_agent_engine import inference
from ai_agent_engine.siwc import CredentialStore


class FakeTransport:
    def __init__(self, *responses: bytes):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        return BytesIO(self.responses.pop(0))


def catalog(*rows):
    return json.dumps({"models": list(rows)}).encode("utf-8")


MODEL_ROWS = catalog(
    {"slug": "gpt-test-1", "display_name": "GPT test", "visibility": "list"},
    {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hidden"},
    {"slug": "gpt-test-1", "display_name": "Duplicate", "visibility": "list"},
)
COMPLETE_STREAM = (
    b'event: response.created\n'
    b'data: {"type":"response.created"}\n\n'
    b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
    b'data: {"type":"response.output_text.delta","delta":", world!"}\n\n'
    b'data: {"type":"response.completed"}\n\n'
    b'data: [DONE]\n\n'
)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = CredentialStore(Path(self.scratch.name) / "private")
        self.store.save_session({
            "client_id": "oaiapp_test",
            "subject": "owner-1",
            "access_token": "super-secret-token",
            "refresh_token": "never-output-refresh",
            "sharing_enabled": True,
            "scopes": ["openid", "resource.invoke", "chatgpt.tokens.use.direct"],
            "expires_at": time.time() + 3600,
        })

    def test_rejects_missing_grant_and_expired_token_without_request(self):
        self.assertEqual(inference._access_token(self.store), "super-secret-token")
        session = self.store.load_session()
        for replacement in (
            {**session, "sharing_enabled": False},
            {**session, "scopes": ["openid"]},
            {**session, "expires_at": time.time() - 1},
            {**session, "access_token": None},
        ):
            with self.subTest(replacement=list(replacement.items())[-1][0]):
                self.store.save_session(replacement)
                with self.assertRaises(inference.InferenceError):
                    inference.list_models(self.store, opener=FakeTransport(MODEL_ROWS))
        self.store.save_session(session)

    def test_lists_only_visible_models_with_saved_oauth_bearer(self):
        transport = FakeTransport(MODEL_ROWS)
        result = inference.list_models(self.store, opener=transport)
        self.assertEqual(result, [("gpt-test-1", "GPT test")])
        request, timeout = transport.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.openai.com/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer super-secret-token")
        self.assertEqual(timeout, 20)

    def test_rejects_unlisted_model_before_inference(self):
        transport = FakeTransport(MODEL_ROWS)
        with self.assertRaisesRegex(inference.InferenceError, "not in"):
            inference.request_answer("gpt-hidden", "hello", self.store, opener=transport)
        self.assertEqual(len(transport.requests), 1)

    def test_streams_stateless_response_and_requires_completion(self):
        transport = FakeTransport(MODEL_ROWS, COMPLETE_STREAM)
        answer = inference.request_answer("gpt-test-1", "Say hello", self.store, opener=transport)
        self.assertEqual(answer, "Hello, world!")
        self.assertEqual(len(transport.requests), 2)
        request, timeout = transport.requests[1]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer super-secret-token")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gpt-test-1")
        self.assertEqual(body["input"], [{"role": "user", "content": "Say hello"}])
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(timeout, 60)

    def test_incomplete_or_interrupted_stream_is_not_success(self):
        cases = (
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            b'data: {"type":"response.incomplete"}\n\n',
            b'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n',
            b'data: not-json\n\n',
        )
        for payload in cases:
            with self.subTest(payload=payload[:30]):
                with self.assertRaises(inference.InferenceError):
                    inference.request_answer("gpt-test-1", "hello", self.store,
                                             opener=FakeTransport(MODEL_ROWS, payload))

    def test_oversized_model_catalog_and_stream_are_rejected(self):
        oversized = b"x" * (inference.MAX_MODEL_RESPONSE_BYTES + 1)
        with self.assertRaisesRegex(inference.InferenceError, "large"):
            inference.list_models(self.store, opener=FakeTransport(oversized))
        with self.assertRaisesRegex(inference.InferenceError, "limit"):
            inference._consume_sse(BytesIO(
                b'data: {"type":"response.output_text.delta","delta":"' +
                b"x" * (inference.MAX_STREAM_BYTES + 1)
            ))

    def test_provider_http_errors_redact_secret(self):
        def unavailable(request, timeout):
            raise HTTPError(request.full_url, 401, "forbidden", None, None)
        with self.assertRaises(inference.InferenceError) as caught:
            inference.list_models(self.store, opener=unavailable)
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("super-secret-token", str(caught.exception))

    def test_http_quota_stops_without_retry_or_changing_session(self):
        requests = []
        def transport(request, timeout):
            requests.append(request)
            if request.full_url.endswith("/models"):
                return BytesIO(MODEL_ROWS)
            raise self._quota_rejection(request)
        from ai_agent_engine.api_errors import PlanRequestError
        before = self.store.load_session()
        with self.assertRaises(PlanRequestError) as caught:
            inference.request_answer("gpt-test-1", "hello", self.store,
                                     opener=transport)
        self.assertEqual(len(requests), 2)
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.body_shape, "error")
        self.assertIn("Pause requests", str(caught.exception))
        self.assertEqual(self.store.load_session(), before)

    @staticmethod
    def _quota_rejection(request):
        return HTTPError(request.full_url, 429, "private provider message", {
            "x-request-id": "req_quota1",
        }, BytesIO(json.dumps({
            "error": {"code": "subscription_sharing_usage_limit_exceeded",
                      "message": "private subscriber details"},
        }).encode("utf-8")))

    def test_failed_stream_code_keeps_diagnostic_without_partial_success(self):
        from ai_agent_engine.api_errors import PlanRequestError
        stream = (
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            b'data: {"type":"response.failed","response":{"error":'
            b'{"code":"subscription_sharing_usage_limit_exceeded",'
            b'"message":"hidden provider details"}}}\n\n'
        )
        with self.assertRaises(PlanRequestError) as caught:
            inference.request_answer("gpt-test-1", "hello", self.store,
                                     opener=FakeTransport(MODEL_ROWS, stream))
        self.assertEqual(caught.exception.body_shape, "stream.error")
        self.assertIn("Pause requests", str(caught.exception))
        self.assertNotIn("hidden provider details", str(caught.exception))

    def test_one_click_probe_chooses_first_visible_model(self):
        transport = FakeTransport(MODEL_ROWS, MODEL_ROWS, COMPLETE_STREAM)
        model, reply = inference.probe_first_model(self.store, opener=transport)
        self.assertEqual(model, "gpt-test-1")
        self.assertEqual(reply, "Hello, world!")
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(transport.requests[-1][0].get_method(), "POST")
        self.assertEqual(json.loads(transport.requests[-1][0].data)["model"],
                         "gpt-test-1")

    def test_one_click_probe_rejects_empty_catalog(self):
        invisible = catalog(
            {"slug": "no", "visibility": "hidden"},
        )
        transport = FakeTransport(invisible)
        with self.assertRaisesRegex(inference.InferenceError, "No visible models"):
            inference.probe_first_model(self.store, opener=transport)
        self.assertEqual(len(transport.requests), 1)

    def test_one_click_cli_is_routed_without_manual_arguments(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch

        output = StringIO()
        with patch.object(inference, "probe_first_model",
                          return_value=("gpt-test-1", "Hello, world!")) as probe:
            with redirect_stdout(output):
                self.assertEqual(inference.main(["probe"]), 0)
        probe.assert_called_once_with()
        self.assertIn("ChatGPT model: gpt-test-1", output.getvalue())
        self.assertIn("Completed GPT reply: Hello, world!", output.getvalue())

    def test_vscode_tasks_are_explicit_and_do_not_autorun(self):
        root = Path(__file__).resolve().parent.parent
        tasks = json.loads((root / ".vscode" / "tasks.json").read_text())
        self.assertEqual(tasks["version"], "2.0.0")
        by_label = {task["label"]: task for task in tasks["tasks"]}
        self.assertEqual(len(by_label), 4)
        self.assertEqual(
            by_label["AI Agent Engine: 3. Send one GPT test request"]["args"],
            ["-m", "ai_agent_engine.inference", "probe"],
        )
        self.assertEqual(
            by_label["AI Agent Engine: 4. GPT writes and checks a safe test file"]["args"],
            ["-m", "ai_agent_engine.plan_agent", "probe"],
        )
        for task in by_label.values():
            self.assertEqual(task["type"], "process")
            self.assertEqual(task["command"], "python3")
            self.assertEqual(task["options"]["cwd"], "${workspaceFolder}")
            self.assertNotEqual(task.get("runOptions", {}).get("runOn"), "folderOpen")
            self.assertNotIn("token", str(task).lower())

    def test_blank_prompts_do_not_call_provider(self):
        with self.assertRaises(inference.InferenceError):
            inference.request_answer("gpt-test-1", " ", self.store,
                                     opener=FakeTransport(MODEL_ROWS))


if __name__ == "__main__":
    unittest.main()
