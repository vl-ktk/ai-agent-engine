"""Owner-triggered GPT/ChatGPT-plan tool-call test with the existing agent engine.

The model can only call two local functions in a disposable directory. This is
not the production agent adapter, a general shell executor or a background job.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import http.client
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.request import urlopen

from .api_errors import stream_plan_error
from .core import Action, Agent, AgentResult, Message, ModelTurn, Tool
from .inference import (
    API_ORIGIN, MAX_STREAM_BYTES, InferenceError, _access_token,
    _authorized_request, _open, list_models,
)
from .session_refresh import SessionRefreshError, refresh_if_needed
from .siwc import CredentialStore
from .tools import SandboxFiles, ToolError

FILE_NAME = "gpt-agent-test.txt"
FILE_CONTENT = "GPT local tool test passed"
MAX_TOOL_ARGUMENT_BYTES = 16_384
MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_TOOL_REQUESTS = 4  # Hard cap on Responses calls in one tool-cycle test.
MAX_AGENT_STEPS = 6    # Queued calls from one response use steps, not requests.
TASK_PROMPT = (
    "Use the sandbox tools to run this test.\n"
    "Step 1: call write_new_file with path set to " + json.dumps(FILE_NAME) +
    " and content set to exactly " + json.dumps(FILE_CONTENT) +
    " (no quotes, no trailing period, no newline).\n"
    "Step 2: after the write succeeds, call read_file with the same path.\n"
    "Step 3: after the read succeeds, reply with one short confirmation sentence."
)

TOOL_PARAMETERS: dict[str, dict[str, Any]] = {
    "write_new_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative filename within the temporary test sandbox."},
            "content": {"type": "string", "description": "UTF-8 text for a new file; never overwrite."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    "read_file": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path inside the test sandbox."}},
        "required": ["path"],
        "additionalProperties": False,
    },
}


def _tool_names(available: tuple[str, ...]) -> list[dict[str, Any]]:
    if not available or len(set(available)) != len(available):
        raise InferenceError("Invalid list of permitted local tools")
    if any(name not in TOOL_PARAMETERS for name in available):
        raise InferenceError("Unrecognized local tool in allowed list")
    return [{
        "type": "namespace",
        "name": "sandbox",
        "description": "Isolated disposable test files only; no shell, network or working repositories.",
        "tools": [{
            "type": "function",
            "name": name,
            "description": ("Create a brand-new text file in the temporary sandbox only."
                            if name == "write_new_file" else
                            "Read an existing text file in the temporary sandbox only."),
            "parameters": TOOL_PARAMETERS[name],
            "strict": True,
        } for name in available],
    }]


def _stream_output(response: Any) -> list[dict[str, Any]]:
    """Accept output only from a completed streamed Responses request."""
    total = 0
    data_lines: list[str] = []
    completed: list[dict[str, Any]] | None = None

    def dispatch() -> None:
        nonlocal completed
        if not data_lines:
            return
        raw = "\n".join(data_lines)
        data_lines.clear()
        if raw == "[DONE]":
            return
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            raise InferenceError("OpenAI returned a malformed tool stream") from None
        if not isinstance(event, dict):
            raise InferenceError("OpenAI returned an invalid tool stream")
        kind = event.get("type")
        if kind == "response.failed":
            raise stream_plan_error(event)
        if kind == "response.incomplete":
            raise InferenceError("OpenAI tool response was incomplete")
        if kind == "response.completed":
            payload = event.get("response")
            output = payload.get("output") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or payload.get("status") != "completed" or
                    not isinstance(output, list) or any(not isinstance(item, dict) for item in output)):
                raise InferenceError("OpenAI completed response contained no valid output")
            completed = output

    for line in response:
        if not isinstance(line, bytes):
            raise InferenceError("OpenAI returned an invalid tool stream")
        total += len(line)
        if total > MAX_STREAM_BYTES:
            raise InferenceError("OpenAI tool stream exceeded its safety limit")
        line = line.rstrip(b"\r\n")
        if not line:
            dispatch()
        elif line.startswith(b"data:"):
            try:
                data_lines.append(line[5:].lstrip(b" ").decode("utf-8"))
            except UnicodeError:
                raise InferenceError("OpenAI tool stream contains invalid encoding") from None
    dispatch()
    if completed is None:
        raise InferenceError("OpenAI stream ended without response.completed")
    return completed


def _decode_action(item: Mapping[str, Any], available: tuple[str, ...]) -> tuple[str, dict[str, str], str]:
    name = item.get("name")
    if isinstance(name, str) and name.startswith("sandbox."):
        name = name[len("sandbox."):]
    if name not in available:
        raise InferenceError("OpenAI requested an unauthorized local tool")
    call_id = item.get("call_id")
    raw_args = item.get("arguments")
    if (not isinstance(call_id, str) or not call_id or len(call_id) > 256 or
            not isinstance(raw_args, str) or len(raw_args.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES):
        raise InferenceError("OpenAI returned an invalid tool call")
    try:
        arguments = json.loads(raw_args)
    except (ValueError, TypeError):
        raise InferenceError("OpenAI returned invalid tool arguments") from None
    expected = set(TOOL_PARAMETERS[name]["required"])
    if (not isinstance(arguments, dict) or set(arguments) != expected or
            any(not isinstance(value, str) for value in arguments.values())):
        raise InferenceError("OpenAI returned invalid tool arguments")
    return name, arguments, call_id


def _final_text(output: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                value = part.get("text")
                if isinstance(value, str):
                    texts.append(value)
    answer = "\n".join(texts).strip()
    if not answer or len(answer) > MAX_TOOL_OUTPUT_CHARS:
        raise InferenceError("OpenAI returned no valid final agent answer")
    return answer


_REPLAY_DROP_KEYS = ("id", "status")


def _replay_items(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return output items that are safe to resend in a stateless request.

    With store=false the server keeps no items, so server item IDs must not be
    referenced. Reasoning items are only replayable with encrypted content,
    which this harness does not request; they are dropped.
    """
    replay: list[dict[str, Any]] = []
    for item in output:
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind in ("function_call", "message"):
            replay.append({k: v for k, v in item.items() if k not in _REPLAY_DROP_KEYS})
    return replay


def _safe_exception(exc: BaseException) -> str:
    """Redacted description for errors that are not already sanitized."""
    if isinstance(exc, (InferenceError, SessionRefreshError)):
        return str(exc)
    return "unexpected " + type(exc).__name__ + " (details withheld)"


class OpenAIPlanModel:
    """Model protocol adapter using native namespaced function calls.

    Function_call items and their call IDs are retained privately in memory and
    sent back with matching function_call_output items. No server-side state.
    Several calls in one response are queued and executed one at a time, then
    all outputs are returned in the next single request.
    """

    def __init__(self, model: str, store: CredentialStore, *,
                 opener: Callable[..., Any] = urlopen,
                 max_requests: int = MAX_TOOL_REQUESTS):
        self.model = model
        self.store = store
        self.opener = opener
        self.max_requests = max_requests
        self.requests_sent = 0
        self.requested_tools: list[str] = []
        self.last_error: str | None = None
        self._input: list[dict[str, Any]] = []
        self._queue: list[tuple[str, dict[str, str], str]] = []
        self._pending: tuple[str, str] | None = None
        self._seen_history = 0

    def complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn:
        try:
            return self._complete(history, allowed_tools)
        except Exception as exc:  # Agent hides exceptions; keep a redacted reason.
            self.last_error = _safe_exception(exc)
            raise

    def _accept_tool_result(self, history: tuple[Message, ...]) -> None:
        if (self._pending is None or len(history) != self._seen_history + 2 or
                history[-2].role != "assistant" or history[-1].role != "tool" or
                history[-2].content != "Called tool: " + self._pending[1]):
            raise InferenceError("Agent tool history does not match the requested call")
        output = history[-1].content
        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            raise InferenceError("Local tool returned too much data")
        self._input.append({
            "type": "function_call_output",
            "call_id": self._pending[0],
            "output": output,
        })
        self._pending = None
        self._seen_history = len(history)

    def _next_queued(self) -> ModelTurn:
        name, arguments, call_id = self._queue.pop(0)
        self._pending = (call_id, name)
        self.requested_tools.append(name)
        return ModelTurn(action=Action(name, arguments))

    def _complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn:
        tool_spec = _tool_names(allowed_tools)
        if self._seen_history == 0:
            if len(history) != 1 or history[0].role != "user":
                raise InferenceError("Invalid initial agent history")
            self._input = [{"role": "user", "content": history[0].content}]
            self._seen_history = 1
        else:
            self._accept_tool_result(history)
            if self._queue:
                return self._next_queued()

        if self.requests_sent >= self.max_requests:
            raise InferenceError("GPT tool test reached its model request limit")
        body = json.dumps({
            "model": self.model,
            "input": self._input,
            "instructions": (
                "You are testing an isolated owner-operated agent. Use only sandbox "
                "tools for file work; no shell, external access or real projects. "
                "Never claim you used a tool unless it actually returned a result. "
                "Call one tool at a time and wait for its result."
            ),
            "tools": tool_spec,
            "store": False,
            "stream": True,
        }).encode("utf-8")
        request = _authorized_request(API_ORIGIN + "/responses", _access_token(self.store), data=body)
        self.requests_sent += 1
        try:
            with _open(request, opener=self.opener, timeout=90) as response:
                output = _stream_output(response)
        except (URLError, TimeoutError, OSError, http.client.HTTPException):
            raise InferenceError("OpenAI tool stream was interrupted") from None
        calls = [item for item in output if item.get("type") == "function_call"]
        decoded = [_decode_action(call, allowed_tools) for call in calls]
        self._input.extend(_replay_items(output))
        if decoded:
            self._queue = list(decoded)
            return self._next_queued()
        return ModelTurn(final_text=_final_text(output))


@dataclass
class ToolCycleReport:
    """Redacted facts from one bounded GPT tool cycle. No content or secrets."""

    model: str
    status: str = "not_started"
    error: str | None = None
    requests_sent: int = 0
    requested_tools: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    write_executed: bool = False
    write_path_ok: bool = False
    write_content_ok: bool = False
    file_exists: bool = False
    file_content_ok: bool = False
    read_executed: bool = False
    read_output_ok: bool = False
    final_answer: bool = False
    audit: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (self.status == "done" and self.write_executed and self.file_content_ok
                and self.read_executed and self.read_output_ok and self.final_answer)


def _recording_tools(files: SandboxFiles, report: ToolCycleReport) -> tuple[Tool, ...]:
    """Wrap sandbox tools to record redacted facts about each execution."""
    write, read = files.allowed_tools()

    def run(tool: Tool, args: Mapping[str, str]) -> str:
        try:
            return tool.execute(args)
        except ToolError as exc:  # Fixed, content-free messages from tools.py.
            report.tool_errors.append(tool.name + ": " + str(exc))
            raise
        except Exception as exc:
            report.tool_errors.append(tool.name + ": " + _safe_exception(exc))
            raise

    def write_new(args: Mapping[str, str]) -> str:
        report.write_path_ok = args.get("path") == FILE_NAME
        report.write_content_ok = args.get("content") == FILE_CONTENT
        output = run(write, args)
        report.write_executed = True
        return output

    def read_file(args: Mapping[str, str]) -> str:
        output = run(read, args)
        report.read_executed = True
        report.read_output_ok = output == FILE_CONTENT
        return output

    return (Tool("write_new_file", write_new), Tool("read_file", read_file))


def run_tool_cycle(slug: str, store: CredentialStore, *,
                   opener: Callable[..., Any] = urlopen) -> ToolCycleReport:
    """One bounded GPT write/read cycle in a disposable directory."""
    report = ToolCycleReport(model=slug)
    with TemporaryDirectory(prefix="ai-agent-gpt-test-") as scratch:
        root = Path(scratch)
        files = SandboxFiles(root)
        model = OpenAIPlanModel(slug, store, opener=opener)
        agent = Agent(model=model, tools=_recording_tools(files, report),
                      max_steps=MAX_AGENT_STEPS)
        result = agent.run(TASK_PROMPT)
        target = root / FILE_NAME
        report.status = result.status
        report.error = model.last_error
        report.requests_sent = model.requests_sent
        report.requested_tools = list(model.requested_tools)
        report.final_answer = result.status == "done" and bool(result.answer)
        report.audit = tuple(
            event.event + (":" + event.tool if event.tool else "") for event in result.audit
        )
        report.file_exists = target.is_file()
        if report.file_exists:
            try:
                report.file_content_ok = target.read_text(encoding="utf-8") == FILE_CONTENT
            except (OSError, UnicodeError):
                report.file_content_ok = False
    return report


def run_one_click(store: CredentialStore | None = None, *,
                  opener: Callable[..., Any] = urlopen) -> tuple[str, ToolCycleReport, bool, str | None]:
    """One voluntary, bounded GPT file-write/read test. All files are deleted."""
    session_store = store or CredentialStore()
    refresh_if_needed(session_store, opener=opener)
    available = list_models(session_store, opener=opener)
    if not available:
        raise InferenceError("No ChatGPT models are available for this account")
    slug = available[0][0]
    report = run_tool_cycle(slug, session_store, opener=opener)
    return slug, report, report.passed, report.error


def describe(report: ToolCycleReport) -> list[str]:
    """Human-readable, secret-free summary lines."""
    return [
        "ChatGPT model: " + report.model,
        "Agent status: " + report.status,
        "Model requests sent: " + str(report.requests_sent),
        "Tools requested by GPT: " + (", ".join(report.requested_tools) or "none"),
        "Audit: " + (", ".join(report.audit) or "none"),
    ] + (["Safe error: " + report.error] if report.error else []) + [
        "Tool error: " + item for item in report.tool_errors
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Owner-triggered GPT sandbox tools test")
    parser.add_argument("command", choices=["probe"])
    parser.parse_args(argv)
    try:
        _, report, passed, _ = run_one_click()
    except (InferenceError, SessionRefreshError) as exc:
        print("GPT sandbox test unavailable:", exc, file=sys.stderr)
        return 1
    for line in describe(report):
        print(line)
    print("Safe file write/read verification:", "PASS" if passed else "FAIL")
    print("Temporary sandbox: automatically removed")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
