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

    def test_blank_prompts_do_not_call_provider(self):
        with self.assertRaises(inference.InferenceError):
            inference.request_answer("gpt-test-1", " ", self.store,
                                     opener=FakeTransport(MODEL_ROWS))


if __name__ == "__main__":
    unittest.main()
