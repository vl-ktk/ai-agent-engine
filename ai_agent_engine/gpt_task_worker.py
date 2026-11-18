"""Trusted single-request ChatGPT-plan subprocess for one task attempt.

Input arrives on private stdin, never argv. The worker uses only the
already configured Sign in with ChatGPT profile and documented Responses
route; it cannot fall back to a paid API key or execute model tools.
"""

from __future__ import annotations

import json
import re
import sys

from .api_errors import InferenceError, PlanRequestError
from .inference import request_answer
from .siwc import AuthError


MAX_REQUEST_BYTES = 20_000
MAX_RESULT_CHARS = 20_000
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


def _safe_error_category(exc: Exception) -> str:
    if isinstance(exc, PlanRequestError):
        if (exc.http_status == 429
                or exc.error_code == "subscription_sharing_usage_limit_exceeded"):
            return "quota"
        if exc.http_status in (401, 403) or exc.error_code in (
                "chatpass_v2_scope_not_authorized",
                "subscription_sharing_user_not_eligible"):
            return "auth"
        if exc.http_status == 400 or exc.error_code in (
                "subscription_sharing_unsupported_capability",
                "subscription_sharing_route_not_supported"):
            return "unsupported"
    return "unavailable"


def execute_request(raw: bytes) -> dict[str, object]:
    """Return only bounded result data; never serialize provider errors."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        raise ValueError("invalid private worker payload")
    try:
        data = json.loads(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid private worker payload") from exc
    if not isinstance(data, dict) or set(data) != {"model_id", "prompt"}:
        raise ValueError("invalid private worker fields")
    model_id, prompt = data["model_id"], data["prompt"]
    if (type(model_id) is not str or not _MODEL.fullmatch(model_id)
            or type(prompt) is not str or not prompt.strip()
            or len(prompt) > 4000):
        raise ValueError("invalid private worker task")
    answer = request_answer(model_id, prompt)
    if (type(answer) is not str or not answer.strip()
            or len(answer) > MAX_RESULT_CHARS):
        raise ValueError("invalid model answer")
    return {"ok": True, "text": answer}


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        result = execute_request(payload)
    except (InferenceError, AuthError, ValueError, OSError) as exc:
        # No URLs, access tokens, prompts, provider response bodies or paths.
        print(json.dumps({"ok": False, "error": _safe_error_category(exc)}),
              flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
