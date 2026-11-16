"""Owner-triggered GPT/ChatGPT-plan tool-call test with the existing agent engine.

The model can only call two local functions in a disposable directory. This is
not the production agent adapter, a general shell executor or a background job.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.request import urlopen

from .core import Action, Agent, AgentResult, Message, ModelTurn
from .inference import (
    API_ORIGIN, MAX_STREAM_BYTES, InferenceError, _access_token,
    _authorized_request, _open, list_models,
)
from .session_refresh import SessionRefreshError, refresh_if_needed
from .siwc import CredentialStore
from .tools import SandboxFiles

FILE_NAME = "gpt-agent-test.txt"
FILE_CONTENT = "GPT local tool test passed"
MAX_TOOL_ARGUMENT_BYTES = 16_384
MAX_TOOL_OUTPUT_CHARS = 20_000

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
            raise InferenceError("OpenAI tool request failed")
        if kind == "response.incomplete":
            raise InferenceError("OpenAI tool response was incomplete")
        if kind == "response.completed":
            payload = event.get("response")
            output = payload.get("output") if isinstance(payload, dict) else None
            if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
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
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text":
                value = part.get("text")
                if isinstance(value, str):
                    texts.append(value)
    answer = "\n".join(texts).strip()
    if not answer or len(answer) > MAX_TOOL_OUTPUT_CHARS:
        raise InferenceError("OpenAI returned no valid final agent answer")
    return answer


class OpenAIPlanModel:
    """Model protocol adapter using native namespaced function calls.

    Function_call items and their call IDs are retained privately in memory and
    sent back with matching function_call_output items. No server-side state.
    """

    def __init__(self, model: str, store: CredentialStore, *,
                 opener: Callable[..., Any] = urlopen):
        self.model = model
        self.store = store
        self.opener = opener
        self._input: list[dict[str, Any]] = []
        self._pending: tuple[str, str] | None = None
        self._seen_history = 0
        self.last_error: str | None = None

    def complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn:
        try:
            return self._complete(history, allowed_tools)
        except (InferenceError, SessionRefreshError) as exc:
            self.last_error = str(exc)
            raise

    def _complete(self, history: tuple[Message, ...], allowed_tools: tuple[str, ...]) -> ModelTurn:
        tool_spec = _tool_names(allowed_tools)
        if self._seen_history == 0:
            if len(history) != 1 or history[0].role != "user":
                raise InferenceError("Invalid initial agent history")
            self._input = [{"role": "user", "content": history[0].content}]
        else:
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

        body = json.dumps({
            "model": self.model,
            "input": self._input,
            "instructions": (
                "You are testing an isolated owner-operated agent. Use only sandbox "
                "tools for file work; no shell, external access or real projects. "
                "Never claim you used a tool unless it actually returned a result. "
                "When asked to check a file, call read_file after writing it."
            ),
            "tools": tool_spec,
            "store": False,
            "stream": True,
        }).encode("utf-8")
        request = _authorized_request(API_ORIGIN + "/responses", _access_token(self.store), data=body)
        try:
            with _open(request, opener=self.opener, timeout=90) as response:
                output = _stream_output(response)
        except (URLError, TimeoutError, OSError):
            raise InferenceError("OpenAI tool stream was interrupted") from None
        calls = [item for item in output if item.get("type") == "function_call"]
        if len(calls) > 1:
            raise InferenceError("OpenAI requested multiple tools in one test step")
        self._input.extend(output)
        self._seen_history = len(history)
        if calls:
            name, arguments, call_id = _decode_action(calls[0], allowed_tools)
            self._pending = (call_id, name)
            return ModelTurn(action=Action(name, arguments))
        return ModelTurn(final_text=_final_text(output))


def run_one_click(store: CredentialStore | None = None, *,
                  opener: Callable[..., Any] = urlopen) -> tuple[str, AgentResult, bool, str | None]:
    """One voluntary, bounded GPT file-write/read test. All files are deleted."""
    session_store = store or CredentialStore()
    refresh_if_needed(session_store, opener=opener)
    available = list_models(session_store, opener=opener)
    if not available:
        raise InferenceError("No ChatGPT models are available for this account")
    slug = available[0][0]
    with TemporaryDirectory(prefix="ai-agent-gpt-test-") as scratch:
        root = Path(scratch)
        files = SandboxFiles(root)
        model = OpenAIPlanModel(slug, session_store, opener=opener)
        agent = Agent(model=model, tools=files.allowed_tools(), max_steps=4)
        result = agent.run(
            "Create a new file named " + FILE_NAME + " with the exact content: " +
            FILE_CONTENT + ". Then read it back with the read_file tool. "
            "After the read succeeds, give a short final confirmation."
        )
        events = {(event.event, event.tool) for event in result.audit}
        passed = (
            result.status == "done" and
            ("tool_ok", "write_new_file") in events and
            ("tool_ok", "read_file") in events and
            (root / FILE_NAME).is_file() and
            (root / FILE_NAME).read_text(encoding="utf-8") == FILE_CONTENT
        )
        return slug, result, passed, model.last_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Owner-triggered GPT sandbox tools test")
    parser.add_argument("command", choices=["probe"])
    parser.parse_args(argv)
    try:
        model, result, passed, error = run_one_click()
    except (InferenceError, SessionRefreshError) as exc:
        print("GPT sandbox test unavailable:", exc, file=sys.stderr)
        return 1
    print("ChatGPT model:", model)
    print("Agent status:", result.status)
    print("Verified tools:", ", ".join(
        event.tool or "-" for event in result.audit if event.event == "tool_ok"
    ) or "none")
    if error:
        print("Safe error:", error)
    print("Safe file write/read verification:", "PASS" if passed else "FAIL")
    print("Temporary sandbox: automatically removed")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
