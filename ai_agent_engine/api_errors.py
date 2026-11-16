"""Redacted diagnostics for ChatGPT-plan Responses and direct admission errors.

Keep metadata needed for recovery without storing untrusted HTTP bodies,
arbitrary server messages, Authorization headers or OAuth credentials.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from urllib.error import HTTPError

MAX_ERROR_BODY_BYTES = 16_384
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.\[\]-]{1,80}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

RECOVERY_BY_CODE = {
    "subscription_sharing_usage_limit_exceeded":
        "ChatGPT plan usage limit reached. Pause requests and check ChatGPT Settings > Usage; do not guess a reset time.",
    "subscription_sharing_user_not_eligible":
        "This ChatGPT account or workspace is not eligible for plan use. Do not retry the same request.",
    "subscription_sharing_usage_unavailable":
        "ChatGPT plan usage is temporarily unavailable. Keep the session and retry later.",
    "subscription_sharing_user_unavailable":
        "ChatGPT subscriber details are temporarily unavailable. Keep the session and retry later.",
    "subscription_sharing_unsupported_capability":
        "This request uses an unsupported capability. Inspect the reported parameter before retrying.",
    "subscription_sharing_route_not_supported":
        "This route is not supported for ChatGPT plan usage. Check the HTTP method and endpoint.",
    "subscription_sharing_invalid_user":
        "ChatGPT subscriber identity was rejected. Check the saved account and authorization.",
    "chatpass_v2_scope_not_authorized":
        "The ChatGPT plan grant does not authorize this operation. Check OAuth permissions.",
    "chatpass_v2_invalid_authorization_context":
        "The ChatGPT authorization context was rejected. Check client and grant configuration.",
}

RECOVERY_BY_STATUS = {
    400: "OpenAI rejected the request. Check its supported input and tools; do not retry unchanged.",
    401: "ChatGPT authorization was rejected. Check the selected account and granted scopes.",
    403: "OpenAI blocked this request for a policy or permission reason. Check the integration.",
    429: "ChatGPT plan request was limited. Pause requests and check ChatGPT Settings > Usage.",
    503: "OpenAI direct routing is temporarily unavailable. Keep credentials and retry later.",
}


class InferenceError(Exception):
    """Expected, safe model diagnostic failure."""


class PlanRequestError(InferenceError):
    """Machine-readable, redacted failure metadata, including HTTP status."""

    def __init__(self, *, http_status: int | None, error_code: str | None,
                 error_param: str | None, body_shape: str,
                 request_id: str | None = None):
        self.http_status = http_status
        self.error_code = error_code
        self.error_param = error_param
        self.body_shape = body_shape
        self.request_id = request_id
        help_text = RECOVERY_BY_CODE.get(error_code) or RECOVERY_BY_STATUS.get(
            http_status, "OpenAI response failed. Check the status and try again only after diagnosing the cause."
        )
        facts = []
        if http_status is not None:
            facts.append(f"HTTP {http_status}")
        if error_code:
            facts.append(f"code={error_code}")
        if error_param:
            facts.append(f"param={error_param}")
        facts.append(f"shape={body_shape}")
        if request_id:
            facts.append(f"request_id={request_id}")
        super().__init__(help_text + " (" + "; ".join(facts) + ")")


def _safe(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _safe_request_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else None


def _fields(payload: Any) -> tuple[str | None, str | None, str]:
    if not isinstance(payload, dict):
        return None, None, "other"
    error = payload.get("error")
    if isinstance(error, dict):
        return _safe(error.get("code")), _safe(error.get("param")), "error"
    if "detail" in payload:
        return None, None, "detail"
    return None, None, "other"


def http_plan_error(exc: HTTPError) -> PlanRequestError:
    """Parse at most 16 KiB; neither persist nor echo provider error text."""
    status = exc.code if type(exc.code) is int else None
    request_id = _safe_request_id(exc.headers.get("x-request-id")) if exc.headers else None
    try:
        raw = exc.read(MAX_ERROR_BODY_BYTES + 1)
        if len(raw) > MAX_ERROR_BODY_BYTES:
            shape, code, param = "oversized", None, None
        elif not raw:
            shape, code, param = "empty", None, None
        else:
            try:
                code, param, shape = _fields(json.loads(raw))
            except (UnicodeError, ValueError, TypeError):
                shape, code, param = "non-json", None, None
    except (OSError, ValueError, AttributeError):
        shape, code, param = "unreadable", None, None
    return PlanRequestError(http_status=status, error_code=code,
                            error_param=param, body_shape=shape, request_id=request_id)


def stream_plan_error(event: Mapping[str, Any]) -> PlanRequestError:
    """Keep structured failure codes from SSE; never surface raw model text."""
    response = event.get("response")
    error = response.get("error") if isinstance(response, dict) else event.get("error")
    code, param, _ = _fields({"error": error})
    return PlanRequestError(http_status=None, error_code=code,
                            error_param=param, body_shape="stream.error")
