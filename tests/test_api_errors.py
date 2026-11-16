"""Offline ChatGPT-plan admission and Responses failure diagnostics."""

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError

from ai_agent_engine.api_errors import (
    PlanRequestError, http_plan_error, stream_plan_error,
)


def rejection(status, payload, headers=None):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode("utf-8")
    return HTTPError("https://api.openai.com/v1/responses", status,
                     "provider PRIVATE error", headers or {}, BytesIO(payload))


class PlanErrorTests(unittest.TestCase):
    def test_quota_code_has_actionable_guidance_and_redacted_metadata(self):
        err = http_plan_error(rejection(429, {
            "error": {
                "code": "subscription_sharing_usage_limit_exceeded",
                "param": "tools[0]",
                "message": "Do not expose PRIVATE_ACCESS_TOKEN or internal text",
            },
        }, {"x-request-id": "req_AbC123"}))
        self.assertIsInstance(err, PlanRequestError)
        self.assertEqual(err.http_status, 429)
        self.assertEqual(err.error_code, "subscription_sharing_usage_limit_exceeded")
        self.assertEqual(err.error_param, "tools[0]")
        self.assertEqual(err.body_shape, "error")
        self.assertEqual(err.request_id, "req_AbC123")
        self.assertIn("Pause requests", str(err))
        self.assertIn("Settings", str(err))
        self.assertNotIn("PRIVATE_ACCESS_TOKEN", str(err))
        self.assertNotIn("reset at", str(err))

    def test_direct_admission_detail_is_shape_only_not_text(self):
        err = http_plan_error(rejection(403, {
            "detail": "private subscriber workspace and bearer token should be hidden",
        }))
        self.assertEqual(err.http_status, 403)
        self.assertEqual(err.body_shape, "detail")
        self.assertIsNone(err.error_code)
        self.assertIn("policy or permission", str(err))
        self.assertNotIn("private subscriber", str(err))

    def test_invalid_provider_identifiers_and_request_id_are_not_reflected(self):
        err = http_plan_error(rejection(400, {
            "error": {
                "code": "unsafe newline\nAuthorization: Bearer SECRET",
                "param": "url=https://evil.example/?key=private",
            },
        }, {"x-request-id": "invalid-request-id\nSecret: foo"}))
        self.assertIsNone(err.error_code)
        self.assertIsNone(err.error_param)
        self.assertIsNone(err.request_id)
        self.assertNotIn("Bearer SECRET", str(err))
        self.assertNotIn("evil.example", str(err))

    def test_status_503_preserves_credentials_without_auto_retry_advice(self):
        err = http_plan_error(rejection(503, {"detail": "temporarily offline"}))
        self.assertIn("Keep credentials", str(err))
        self.assertIn("retry later", str(err))
        self.assertEqual(err.http_status, 503)

    def test_empty_invalid_or_oversized_bodies_have_no_contents_in_diagnostics(self):
        for body, expected in (
            (b"", "empty"), (b"TOP_SECRET non-json", "non-json"),
            (b"x" * 16_385, "oversized"),
        ):
            with self.subTest(expected=expected):
                err = http_plan_error(rejection(502, body))
                self.assertEqual(err.body_shape, expected)
                self.assertNotIn("TOP_SECRET", str(err))
                self.assertNotIn("x" * 200, str(err))

    def test_sse_failure_preserves_safe_machine_code_only(self):
        err = stream_plan_error({
            "type": "response.failed",
            "response": {"error": {
                "code": "subscription_sharing_usage_limit_exceeded",
                "param": "model",
                "message": "sensitive bearer token",
            }},
        })
        self.assertIsNone(err.http_status)
        self.assertEqual(err.error_param, "model")
        self.assertEqual(err.body_shape, "stream.error")
        self.assertIn("Pause requests", str(err))
        self.assertNotIn("sensitive", str(err))

    def test_missing_error_object_still_fails_safely(self):
        err = stream_plan_error({"type": "response.failed", "response": {}})
        self.assertIsNone(err.error_code)
        self.assertEqual(err.body_shape, "stream.error")
        self.assertIn("OpenAI response failed", str(err))


if __name__ == "__main__":
    unittest.main()
