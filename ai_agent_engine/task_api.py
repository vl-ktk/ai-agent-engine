"""Stage 5.2: scoped task and subtask identities on loopback.

Submissions stay in a process-local inbox. Owner identity is bound by trusted
server configuration, NOT HTTP input. This is not client authentication,
durable storage, scheduling or model execution.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Lock
from typing import Type
from uuid import uuid4


TASK_PATH = "/v1/tasks"
MAX_BODY_BYTES = 49_152
MAX_TITLE_CHARS = 1_024
MAX_PROMPT_CHARS = 32_768
DEFAULT_MAX_PENDING = 128
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REQUIRED_FIELDS = frozenset({"project_id", "title", "prompt"})
_OPTIONAL_FIELDS = frozenset({"parent_task_id"})
_TASK_ID = re.compile(r"(?:task|subtask)_[0-9a-f]{32}\Z")


class InvalidJSON(ValueError):
    """Malformed, duplicated or non-UTF-8 JSON input."""


class InvalidTask(ValueError):
    """Task fields do not match the narrow admission contract."""


class InboxFull(RuntimeError):
    """The process-local inbox cannot accept more tasks."""


class ProjectForbidden(PermissionError):
    """The server-side owner scope has no access to this project."""


class ParentNotFound(LookupError):
    """Parent is absent or belongs to a different owner/project."""


@dataclass(frozen=True)
class TaskSubmission:
    project_id: str
    title: str
    prompt: str
    parent_task_id: str | None = None


@dataclass(frozen=True)
class OwnerScope:
    """Trusted server configuration; never construct from an HTTP request."""

    owner_id: str
    project_ids: frozenset[str]

    def __post_init__(self) -> None:
        if type(self.owner_id) is not str or not _PROJECT_ID.fullmatch(self.owner_id):
            raise ValueError("invalid configured owner identifier")
        if (type(self.project_ids) is not frozenset or not self.project_ids
                or any(type(p) is not str or not _PROJECT_ID.fullmatch(p)
                       for p in self.project_ids)):
            raise ValueError("invalid configured project scope")


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    owner_id: str
    receipt: int
    submission: TaskSubmission

    @property
    def project_id(self) -> str:
        return self.submission.project_id

    @property
    def parent_task_id(self) -> str | None:
        return self.submission.parent_task_id

    @property
    def kind(self) -> str:
        return "subtask" if self.parent_task_id is not None else "task"

    def metadata(self) -> dict[str, object]:
        """Safe HTTP view: no title, prompt or mutable execution details."""
        return {
            "task_id": self.task_id,
            "owner_id": self.owner_id,
            "project_id": self.project_id,
            "parent_task_id": self.parent_task_id,
            "kind": self.kind,
        }


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
    if (not isinstance(payload, dict)
            or not _REQUIRED_FIELDS <= set(payload)
            or not set(payload) <= _REQUIRED_FIELDS | _OPTIONAL_FIELDS):
        raise InvalidTask("required task fields do not match")
    project_id = payload["project_id"]
    title = payload["title"]
    prompt = payload["prompt"]
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise InvalidTask("invalid project identifier")
    parent_task_id = payload.get("parent_task_id")
    if "parent_task_id" in payload and (
            type(parent_task_id) is not str or not _TASK_ID.fullmatch(parent_task_id)):
        raise InvalidTask("invalid parent task identifier")
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
    return TaskSubmission(project_id=project_id, title=title, prompt=prompt,
                          parent_task_id=parent_task_id)


class TaskInbox:
    """Thread-safe volatile records with scoped access and random task IDs."""

    def __init__(self, *, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 4096:
            raise ValueError("invalid inbox capacity")
        self._lock = Lock()
        self._pending: list[TaskRecord] = []
        self._by_id: dict[str, TaskRecord] = {}
        self._next_receipt = 1
        self._max_pending = max_pending

    def accept(self, submission: TaskSubmission, scope: OwnerScope) -> TaskRecord:
        if not isinstance(submission, TaskSubmission):
            raise TypeError("expected a validated TaskSubmission")
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        with self._lock:
            if submission.project_id not in scope.project_ids:
                raise ProjectForbidden("project outside configured scope")
            if submission.parent_task_id is not None:
                parent = self._by_id.get(submission.parent_task_id)
                if (parent is None or parent.owner_id != scope.owner_id
                        or parent.project_id != submission.project_id):
                    raise ParentNotFound("parent unavailable in owner scope")
            if len(self._pending) >= self._max_pending:
                raise InboxFull("inbox capacity reached")
            receipt = self._next_receipt
            self._next_receipt += 1
            prefix = "subtask_" if submission.parent_task_id is not None else "task_"
            task_id = prefix + uuid4().hex
            # UUID4 collisions are astronomically unlikely, but never overwrite.
            while task_id in self._by_id:
                task_id = prefix + uuid4().hex
            record = TaskRecord(task_id=task_id, owner_id=scope.owner_id,
                                receipt=receipt, submission=submission)
            self._pending.append(record)
            self._by_id[task_id] = record
            return record

    def snapshot(self, scope: OwnerScope) -> tuple[TaskRecord, ...]:
        """Only inspect records belonging to the configured owner/projects."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        with self._lock:
            return tuple(record for record in self._pending
                         if record.owner_id == scope.owner_id
                         and record.project_id in scope.project_ids)

    def get(self, task_id: str, scope: OwnerScope) -> TaskRecord | None:
        """Indistinguishable missing/foreign IDs avoid revealing other tasks."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return None
        with self._lock:
            record = self._by_id.get(task_id)
            if (record is None or record.owner_id != scope.owner_id
                    or record.project_id not in scope.project_ids):
                return None
            return record


def task_handler(inbox: TaskInbox, scope: OwnerScope) -> Type[BaseHTTPRequestHandler]:
    """Return a handler bound to an inbox AND a trusted owner/project scope."""
    if not isinstance(inbox, TaskInbox) or not isinstance(scope, OwnerScope):
        raise TypeError("expected TaskInbox and trusted OwnerScope")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AI-Agent-Engine-Intake"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            # Do not print untrusted paths, submitted prompts or bearer material.
            return

        def _reply(self, status: int, code: str, **fields: object) -> None:
            payload: dict[str, object] = {"status": code}
            payload.update(fields)
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
                record = inbox.accept(submission, scope)
            except ProjectForbidden:
                self._reply(403, "project_forbidden")
                return
            except ParentNotFound:
                self._reply(404, "parent_not_found")
                return
            except InboxFull:
                self._reply(503, "inbox_full")
                return
            self._reply(202, "accepted_in_memory", receipt=record.receipt,
                        **record.metadata())

        def do_GET(self) -> None:
            if self.path == TASK_PATH:
                self._reply(405, "method_not_allowed")
                return
            if not self.path.startswith(TASK_PATH + "/"):
                self._reply(404, "not_found")
                return
            task_id = self.path[len(TASK_PATH) + 1:]
            record = inbox.get(task_id, scope)
            if record is None:
                self._reply(404, "not_found")
                return
            self._reply(200, "found", **record.metadata())

        def do_PUT(self) -> None:
            self._reply(405, "method_not_allowed")

        def do_DELETE(self) -> None:
            self._reply(405, "method_not_allowed")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental loopback-only task intake")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--owner-id", required=True,
                        help="trusted server-side owner identity")
    parser.add_argument("--project-id", action="append", required=True,
                        help="allowed project ID; repeat for multiple projects")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    try:
        scope = OwnerScope(args.owner_id, frozenset(args.project_id))
    except ValueError as exc:
        parser.error(str(exc))
    with ThreadingHTTPServer(("127.0.0.1", args.port),
                             task_handler(TaskInbox(), scope)) as server:
        print(f"task intake listening on 127.0.0.1:{args.port} (scoped, volatile, no client auth)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
