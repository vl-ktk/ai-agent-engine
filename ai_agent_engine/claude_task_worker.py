"""One owner-only Claude Code Pro request through the official, unmodified CLI.

No API key, OAuth proxy, persistent Claude session, tools, MCP, or paid API
fallback. The parent owns the task, permissions, timeout and durable audit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys


MAX_REQUEST_BYTES = 20_000
MAX_RESULT_CHARS = 20_000
MAX_CLI_BYTES = 100_000
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


class ClaudeWorkerError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("Claude task worker could not complete")


def _json_result(raw: bytes) -> dict:
    if len(raw) > MAX_CLI_BYTES:
        raise ClaudeWorkerError("invalid_result")
    try:
        result = json.loads(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError):
        raise ClaudeWorkerError("invalid_result") from None
    if type(result) is not dict:
        raise ClaudeWorkerError("invalid_result")
    return result


def _safe_failure(result: dict) -> str:
    status = result.get("api_error_status")
    if type(status) is int:
        if status == 429:
            return "quota"
        if status in (401, 403):
            return "auth"
        if status == 400:
            return "unsupported"
    return "unavailable"


def _run(executable: str, args: list[str], *, input_bytes: bytes | None,
         timeout: int) -> dict:
    try:
        result = subprocess.run([executable, *args], input=input_bytes,
                                capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise ClaudeWorkerError("unavailable") from None
    parsed = _json_result(result.stdout)
    if result.returncode != 0:
        raise ClaudeWorkerError(_safe_failure(parsed))
    return parsed


def execute_request(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        raise ClaudeWorkerError("invalid_result")
    try:
        data = json.loads(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError):
        raise ClaudeWorkerError("invalid_result") from None
    if type(data) is not dict or set(data) != {"model_id", "prompt"}:
        raise ClaudeWorkerError("invalid_result")
    model_id, prompt = data["model_id"], data["prompt"]
    if (type(model_id) is not str or not _MODEL.fullmatch(model_id)
            or type(prompt) is not str or not prompt.strip()
            or len(prompt) > 4000):
        raise ClaudeWorkerError("invalid_result")
    executable = shutil.which("claude")
    if not executable:
        raise ClaudeWorkerError("unavailable")

    # Status is read-only. Reject Console/API and third-party routing before
    # transmitting the task, even if an installed Claude binary is present.
    status = _run(executable, ["auth", "status"], input_bytes=None, timeout=15)
    if (status.get("authMethod") != "claude.ai"
            or status.get("loggedIn") is not True
            or status.get("apiProvider") not in (None, "firstParty")
            or status.get("subscriptionType") not in ("pro", "max")):
        raise ClaudeWorkerError("auth")

    args = ["-p", "--output-format", "json",
            "--no-session-persistence", "--tools", "",
            "--strict-mcp-config", "--permission-mode", "plan",
            "--no-chrome", "--max-turns", "1"]
    if model_id != "default":
        args.extend(["--model", model_id])
    result = _run(executable, args, input_bytes=prompt.encode("utf-8"),
                  timeout=150)
    if (result.get("type") != "result" or result.get("subtype") != "success"
            or result.get("is_error") is not False):
        raise ClaudeWorkerError(_safe_failure(result))
    reply = result.get("result")
    if type(reply) is not str or not reply.strip() or len(reply) > MAX_RESULT_CHARS:
        raise ClaudeWorkerError("invalid_result")
    return {"ok": True, "text": reply}


def main() -> int:
    try:
        request = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        result = execute_request(request)
    except ClaudeWorkerError as exc:
        print(json.dumps({"ok": False, "error": exc.category}), flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
