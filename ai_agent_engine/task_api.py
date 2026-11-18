"""Stage 5.1: bounded, loopback-only HTTP task admission.

Submissions stay in a process-local inbox. This is not a durable queue,
authenticated production API, task scheduler or model execution endpoint.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Lock
from typing import Type


TASK_PATH = "/v1/tasks"
MAX_BODY_BYTES = 49_152
MAX_TITLE_CHARS = 1_024
MAX_PROMPT_CHARS = 32_768
DEFAULT_MAX_PENDING = 128
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REQUIRED_FIELDS = frozenset({"project_id", "title", "prompt"})


class InvalidJSON(ValueError):
    """Malformed, duplicated or non-UTF-8 JSON input."""


class InvalidTask(ValueError):
    """Task fields do not match the narrow admission contract."""


class InboxFull(RuntimeError):
    """The process-local inbox cannot accept more tasks."""


@dataclass(frozen=True)
class TaskSubmission:
    project_id: str
    title: str
    prompt: str


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidJSON("duplicate JSON key")
        result[key] = value
    return result


def parse_submission(data: bytes) -> TaskSubmission:
    """Validate exact fields, types and UTF-8; never execute input."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BODY_BYTES:
        raise InvalidJSON("invalid JSON body size")
    try:
        payload = json.loads(
            data.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidJSON("invalid JSON request") from exc
    if not isinstance(payload, dict) or set(payload) != _REQUIRED_FIELDS:
        raise InvalidTask("required task fields do not match")
    project_id = payload["project_id"]
    title = payload["title"]
    prompt = payload["prompt"]
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise InvalidTask("invalid project identifier")
    if (not isinstance(title, str) or not title.strip()
            or len(title) > MAX_TITLE_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in title)):
        raise InvalidTask("invalid title")
    if (not isinstance(prompt, str) or not prompt.strip()
            or len(prompt) > MAX_PROMPT_CHARS or "\x00" in prompt):
        raise InvalidTask("invalid prompt")
    try:
        title.encode("utf-8", errors="strict")
        prompt.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise InvalidTask("invalid UTF-8 text") from exc
    return TaskSubmission(project_id=project_id, title=title, prompt=prompt)


class TaskInbox:
    """Thread-safe volatile admission; receipts are NOT stable task IDs."""

    def __init__(self, *, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 4096:
            raise ValueError("invalid inbox capacity")
        self._lock = Lock()
        self._pending: list[TaskSubmission] = []
        self._next_receipt = 1
        self._max_pending = max_pending

    def accept(self, submission: TaskSubmission) -> int:
        if not isinstance(submission, TaskSubmission):
            raise TypeError("expected a validated TaskSubmission")
        with self._lock:
            if len(self._pending) >= self._max_pending:
                raise InboxFull("inbox capacity reached")
            receipt = self._next_receipt
            self._next_receipt += 1
            self._pending.append(submission)
            return receipt

    def snapshot(self) -> tuple[TaskSubmission, ...]:
        """Inspect the volatile inbox without exposing it over HTTP."""
        with self._lock:
            return tuple(self._pending)


def task_handler(inbox: TaskInbox) -> Type[BaseHTTPRequestHandler]:
    """Return an HTTP handler bound to one explicitly provided inbox."""
    if not isinstance(inbox, TaskInbox):
        raise TypeError("expected a TaskInbox")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AI-Agent-Engine-Intake"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            # Do not print untrusted paths, submitted prompts or bearer material.
            return

        def _reply(self, status: int, code: str, *, receipt: int | None = None) -> None:
            payload: dict[str, object] = {"status": code}
            if receipt is not None:
                payload["receipt"] = receipt
            body = json.dumps(payload, separators=(",", ":")).encode("ascii")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != TASK_PATH:
                self._reply(404, "not_found")
                return
            if self.headers.get("Transfer-Encoding") is not None:
                self._reply(400, "invalid_length")
                return
            lengths = self.headers.get_all("Content-Length") or []
            if not lengths:
                self._reply(411, "length_required")
                return
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self._reply(400, "invalid_length")
                return
            if len(lengths[0]) > 5:
                self._reply(413, "body_too_large")
                return
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                self._reply(413, "body_too_large")
                return
            if length == 0:
                self._reply(400, "invalid_json")
                return
            media_type = self.headers.get("Content-Type", "").lower().replace(" ", "")
            if media_type not in ("application/json", "application/json;charset=utf-8"):
                self._reply(415, "unsupported_media_type")
                return
            try:
                self.connection.settimeout(5)
                data = self.rfile.read(length)
            except OSError:
                self.close_connection = True
                return
            if len(data) != length:
                self._reply(400, "incomplete_body")
                return
            try:
                submission = parse_submission(data)
            except InvalidJSON:
                self._reply(400, "invalid_json")
                return
            except InvalidTask:
                self._reply(422, "invalid_task")
                return
            try:
                receipt = inbox.accept(submission)
            except InboxFull:
                self._reply(503, "inbox_full")
                return
            self._reply(202, "accepted_in_memory", receipt=receipt)

        def do_GET(self) -> None:
            self._reply(405, "method_not_allowed")

        def do_PUT(self) -> None:
            self._reply(405, "method_not_allowed")

        def do_DELETE(self) -> None:
            self._reply(405, "method_not_allowed")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental loopback-only task intake")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    with ThreadingHTTPServer(("127.0.0.1", args.port), task_handler(TaskInbox())) as server:
        print(f"task intake listening on 127.0.0.1:{args.port} (volatile, no auth)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
