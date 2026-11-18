"""Stages 5.1-5.7: scoped intake, durable ordering and task lifecycle.

Owner identity is bound by trusted server configuration, NOT HTTP input.
Use --db for SQLite persistence; without it the inbox stays in memory.
This is not client authentication, execution or worker scheduling.
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

from .task_assignment import (
    AlreadyAssigned, InvalidAssignment, TaskAssignment, WorkspaceRoot,
    validate_worker, validate_workspace_name,
)
from .task_dag import DependencyCycle, TaskDAG
from .task_state import TaskState
from .task_storage import SQLiteTaskStore, StorageUnavailable


TASK_PATH = "/v1/tasks"
MAX_BODY_BYTES = 49_152
MAX_TITLE_CHARS = 1_024
MAX_PROMPT_CHARS = 32_768
DEFAULT_MAX_PENDING = 128
MAX_DEPENDENCIES = 64
MAX_PRIORITY = 100
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REQUIRED_FIELDS = frozenset({"project_id", "title", "prompt"})
_OPTIONAL_FIELDS = frozenset({"parent_task_id", "priority"})
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


class TaskNotFound(LookupError):
    """Target task is absent or outside the owner's scope."""


class DependencyNotFound(LookupError):
    """A prerequisite is absent or outside the target task's scope."""


@dataclass(frozen=True)
class TaskSubmission:
    project_id: str
    title: str
    prompt: str
    parent_task_id: str | None = None
    priority: int = 0


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


@dataclass(frozen=True)
class TaskChoice:
    """Atomic snapshot of one eligible task, not a reservation or execution."""

    task_id: str
    project_id: str
    priority: int
    receipt: int


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidJSON("duplicate JSON key")
        result[key] = value
    return result


def _decode_json(data: bytes) -> object:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BODY_BYTES:
        raise InvalidJSON("invalid JSON body size")
    try:
        return json.loads(data.decode("utf-8", errors="strict"),
                          object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidJSON("invalid JSON request") from exc


def parse_submission(data: bytes) -> TaskSubmission:
    """Validate exact fields, types and UTF-8; never execute input."""
    payload = _decode_json(data)
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
    priority = payload.get("priority", 0)
    if type(priority) is not int or not 0 <= priority <= MAX_PRIORITY:
        raise InvalidTask("invalid priority")
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
                          parent_task_id=parent_task_id, priority=priority)


def parse_dependency_update(data: bytes) -> tuple[str, ...]:
    """Accept exactly {'depends_on': [canonical task IDs]}, up to 64 edges."""
    payload = _decode_json(data)
    if not isinstance(payload, dict) or set(payload) != {"depends_on"}:
        raise InvalidTask("expected depends_on field")
    identifiers = payload["depends_on"]
    if (type(identifiers) is not list or len(identifiers) > MAX_DEPENDENCIES
            or any(type(identifier) is not str or not _TASK_ID.fullmatch(identifier)
                   for identifier in identifiers)
            or len(identifiers) != len(set(identifiers))):
        raise InvalidTask("invalid dependency identifiers")
    return tuple(identifiers)


def parse_priority_update(data: bytes) -> int:
    """Require an exact JSON object with an integer priority from 0 to 100."""
    payload = _decode_json(data)
    if not isinstance(payload, dict) or set(payload) != {"priority"}:
        raise InvalidTask("expected priority field")
    priority = payload["priority"]
    if type(priority) is not int or not 0 <= priority <= MAX_PRIORITY:
        raise InvalidTask("invalid priority")
    return priority


def parse_assignment_request(data: bytes) -> tuple[str, str]:
    """Strict local-only worker and workspace identifiers, not arbitrary paths."""
    payload = _decode_json(data)
    if not isinstance(payload, dict) or set(payload) != {"worker_id", "workspace_name"}:
        raise InvalidAssignment("expected worker_id and workspace_name")
    worker_id, workspace_name = payload["worker_id"], payload["workspace_name"]
    validate_worker(worker_id)
    validate_workspace_name(workspace_name)
    return worker_id, workspace_name


class TaskInbox:
    """Thread-safe scoped inbox, optionally persisted with synchronous SQLite."""

    def __init__(self, *, max_pending: int = DEFAULT_MAX_PENDING,
                 store: SQLiteTaskStore | None = None,
                 workspace_root: WorkspaceRoot | None = None) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 4096:
            raise ValueError("invalid inbox capacity")
        self._lock = Lock()
        self._pending: list[TaskRecord] = []
        self._by_id: dict[str, TaskRecord] = {}
        self._dag = TaskDAG()
        self._priorities: dict[str, int] = {}
        self._assignments: dict[str, TaskAssignment] = {}
        self._next_receipt = 1
        self._max_pending = max_pending
        if store is not None and not isinstance(store, SQLiteTaskStore):
            raise TypeError("expected SQLiteTaskStore")
        if workspace_root is not None and not isinstance(workspace_root, WorkspaceRoot):
            raise TypeError("expected trusted WorkspaceRoot")
        self._store = store
        self._workspace_root = workspace_root
        if store is not None:
            try:
                self._restore()
            except Exception:
                store.close()
                raise

    def _restore(self) -> None:
        """Fail closed on invalid persisted owners, edges or cycles."""
        if self._store is None:
            return
        try:
            rows, edges, claims = self._store.load()
            for (task_id, receipt, owner_id, project_id, parent_task_id,
                 title, prompt, submitted_priority, priority) in rows:
                if (type(task_id) is not str or not _TASK_ID.fullmatch(task_id)
                        or type(receipt) is not int or receipt < 1
                        or task_id in self._by_id
                        or type(priority) is not int or not 0 <= priority <= MAX_PRIORITY):
                    raise StorageUnavailable("invalid persisted task identity")
                OwnerScope(owner_id, frozenset({project_id}))
                payload: dict[str, object] = {
                    "project_id": project_id, "title": title, "prompt": prompt,
                    "priority": submitted_priority,
                }
                if parent_task_id is not None:
                    payload["parent_task_id"] = parent_task_id
                    parent = self._by_id.get(parent_task_id)
                    if (parent is None or parent.owner_id != owner_id
                            or parent.project_id != project_id):
                        raise StorageUnavailable("invalid persisted parent")
                submission = parse_submission(json.dumps(payload,
                    ensure_ascii=False).encode("utf-8"))
                record = TaskRecord(task_id=task_id, receipt=receipt,
                                    owner_id=owner_id, submission=submission)
                self._dag.add_task(task_id, receipt)
                self._pending.append(record)
                self._by_id[task_id] = record
                self._priorities[task_id] = priority
                self._next_receipt = max(self._next_receipt, receipt + 1)
            grouped: dict[str, list[str]] = {}
            for task_id, dependency_id in edges:
                target = self._by_id.get(task_id)
                dependency = self._by_id.get(dependency_id)
                if (target is None or dependency is None
                        or target.owner_id != dependency.owner_id
                        or target.project_id != dependency.project_id):
                    raise StorageUnavailable("invalid persisted dependency scope")
                grouped.setdefault(task_id, []).append(dependency_id)
            for task_id, dependencies in grouped.items():
                if len(dependencies) > MAX_DEPENDENCIES:
                    raise StorageUnavailable("too many persisted dependencies")
                self._dag.set_dependencies(task_id, tuple(dependencies))
            for task_id, worker_id, workspace_name, workspace_dir in claims:
                if task_id not in self._by_id or task_id in self._assignments:
                    raise StorageUnavailable("invalid persisted assignment target")
                validate_worker(worker_id)
                validate_workspace_name(workspace_name)
                if (type(workspace_dir) is not str
                        or not workspace_dir.startswith("/")
                        or workspace_dir.rsplit("/", 1)[-1] != workspace_name):
                    raise StorageUnavailable("invalid persisted workspace path")
                if self._workspace_root is not None:
                    self._workspace_root.verify_loaded(workspace_name, workspace_dir)
                self._assignments[task_id] = TaskAssignment(
                    task_id, worker_id, workspace_name, workspace_dir)
        except (ValueError, TypeError, UnicodeError, OverflowError,
                DependencyCycle, InvalidJSON, InvalidTask) as exc:
            raise StorageUnavailable("invalid persisted task graph") from exc

    def accept(self, submission: TaskSubmission, scope: OwnerScope) -> TaskRecord:
        if not isinstance(submission, TaskSubmission):
            raise TypeError("expected a validated TaskSubmission")
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(submission.priority) is not int or not 0 <= submission.priority <= MAX_PRIORITY:
            raise InvalidTask("invalid priority")
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
            prefix = "subtask_" if submission.parent_task_id is not None else "task_"
            task_id = prefix + uuid4().hex
            # UUID4 collisions are astronomically unlikely, but never overwrite.
            while task_id in self._by_id:
                task_id = prefix + uuid4().hex
            record = TaskRecord(task_id=task_id, owner_id=scope.owner_id,
                                receipt=receipt, submission=submission)
            if self._store is not None:
                self._store.insert_task(record)  # commit before admission response
            self._next_receipt += 1
            self._pending.append(record)
            self._by_id[task_id] = record
            self._dag.add_task(task_id, receipt)
            self._priorities[task_id] = submission.priority
            return record

    def claim_task(self, task_id: str, worker_id: str, workspace_name: str,
                   scope: OwnerScope) -> TaskAssignment:
        """First writer wins: bind worker AND private directory atomically."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            raise TaskNotFound("invalid task identifier")
        validate_worker(worker_id)
        validate_workspace_name(workspace_name)
        with self._lock:
            self._scoped_record(task_id, scope)
            if task_id in self._assignments:
                raise AlreadyAssigned("task already has a worker and workspace")
            if self._workspace_root is None:
                raise InvalidAssignment("trusted workspace root not configured")
            workspace_dir = self._workspace_root.resolve(workspace_name)
            assignment = TaskAssignment(task_id, worker_id, workspace_name,
                                        workspace_dir)
            if self._store is not None:
                self._store.insert_assignment(assignment)
            self._assignments[task_id] = assignment
            return assignment

    def get_assignment(self, task_id: str, scope: OwnerScope) -> TaskAssignment | None:
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            return None
        with self._lock:
            try:
                self._scoped_record(task_id, scope)
            except TaskNotFound:
                return None
            return self._assignments.get(task_id)

    def state(self, task_id: str, scope: OwnerScope) -> TaskState | None:
        """Actual 5.7 state, derived from durable admission/immutable claim.

        No RUNNING or terminal state is fabricated before a trusted execution
        transition API and its durable audit are implemented in later stages.
        """
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            return None
        with self._lock:
            try:
                self._scoped_record(task_id, scope)
            except TaskNotFound:
                return None
            return (TaskState.ASSIGNED if task_id in self._assignments
                    else TaskState.QUEUED)

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

    def _scoped_record(self, task_id: str, scope: OwnerScope) -> TaskRecord:
        """Call only with the inbox lock held."""
        record = self._by_id.get(task_id)
        if (record is None or record.owner_id != scope.owner_id
                or record.project_id not in scope.project_ids):
            raise TaskNotFound("task unavailable in owner scope")
        return record

    def set_dependencies(self, task_id: str, prerequisites: tuple[str, ...],
                         scope: OwnerScope) -> tuple[str, ...]:
        """Validate scope, cross-project edges and cycles under one lock."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            raise TaskNotFound("invalid task identifier")
        if (type(prerequisites) is not tuple or len(prerequisites) > MAX_DEPENDENCIES
                or any(type(dep) is not str or not _TASK_ID.fullmatch(dep)
                       for dep in prerequisites)
                or len(prerequisites) != len(set(prerequisites))):
            raise InvalidTask("invalid prerequisite list")
        with self._lock:
            target = self._scoped_record(task_id, scope)
            for dependency_id in prerequisites:
                dependency = self._by_id.get(dependency_id)
                if (dependency is None or dependency.owner_id != target.owner_id
                        or dependency.project_id != target.project_id):
                    raise DependencyNotFound("prerequisite unavailable in task scope")
            self._dag.validate_dependencies(task_id, prerequisites)
            if self._store is not None:
                self._store.replace_dependencies(task_id, prerequisites)
            self._dag.set_dependencies(task_id, prerequisites)
            return self._dag.dependencies(task_id)

    def dependencies(self, task_id: str, scope: OwnerScope) -> tuple[str, ...]:
        """Return prerequisites only for an owned task."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        with self._lock:
            self._scoped_record(task_id, scope)
            return self._dag.dependencies(task_id)

    def topological_order(self, project_id: str, scope: OwnerScope) -> tuple[str, ...]:
        """Return project tasks with all prerequisites before dependents.

        Stable admission-order tie breaking is deliberately not prioritization.
        """
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(project_id) is not str or not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid project identifier")
        if project_id not in scope.project_ids:
            raise ProjectForbidden("project outside configured scope")
        with self._lock:
            ids = tuple(record.task_id for record in self._pending
                        if record.owner_id == scope.owner_id
                        and record.project_id == project_id)
            return self._dag.topological_order(ids)

    def priority(self, task_id: str, scope: OwnerScope) -> int:
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        with self._lock:
            self._scoped_record(task_id, scope)
            return self._priorities[task_id]

    def set_priority(self, task_id: str, priority: int, scope: OwnerScope) -> int:
        """Atomically reprioritize one owned task, without mutating its ID."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(priority) is not int or not 0 <= priority <= MAX_PRIORITY:
            raise InvalidTask("invalid priority")
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            raise TaskNotFound("invalid task identifier")
        with self._lock:
            self._scoped_record(task_id, scope)
            if self._store is not None:
                self._store.update_priority(task_id, priority)
            self._priorities[task_id] = priority
            return priority

    def close(self) -> None:
        """Release the exclusive SQLite writer lock; never delete stored data."""
        with self._lock:
            if self._store is not None:
                self._store.close()

    @property
    def durable(self) -> bool:
        return self._store is not None

    def select_next(self, project_id: str, scope: OwnerScope,
                    *, completed_task_ids: tuple[str, ...] = ()) -> TaskChoice | None:
        """Pure selection: eligible unfinished task with highest priority.

        A trusted scheduler may supply completed IDs once the state machine
        exists (5.7). HTTP never accepts client-claimed completion. Selection
        does not reserve, transition, execute or remove any task.
        """
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(project_id) is not str or not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid project identifier")
        if project_id not in scope.project_ids:
            raise ProjectForbidden("project outside configured scope")
        if (type(completed_task_ids) is not tuple
                or any(type(task_id) is not str or not _TASK_ID.fullmatch(task_id)
                       for task_id in completed_task_ids)
                or len(completed_task_ids) != len(set(completed_task_ids))):
            raise InvalidTask("invalid completed task IDs")
        with self._lock:
            for task_id in completed_task_ids:
                record = self._scoped_record(task_id, scope)
                if record.project_id != project_id:
                    raise TaskNotFound("completed task outside requested project")
            completed = set(completed_task_ids)
            ready = [record for record in self._pending
                     if record.owner_id == scope.owner_id
                     and record.project_id == project_id
                     and record.task_id not in completed
                     and record.task_id not in self._assignments
                     and all(dep in completed
                             for dep in self._dag.dependencies(record.task_id))]
            if not ready:
                return None
            winner = min(ready, key=lambda r: (-self._priorities[r.task_id],
                                               r.receipt, r.task_id))
            return TaskChoice(task_id=winner.task_id, project_id=project_id,
                              priority=self._priorities[winner.task_id],
                              receipt=winner.receipt)


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

        def _read_body(self) -> bytes | None:
            """Shared bounded HTTP body parsing for POST and PUT."""
            if self.headers.get("Transfer-Encoding") is not None:
                self._reply(400, "invalid_length")
                return None
            lengths = self.headers.get_all("Content-Length") or []
            if not lengths:
                self._reply(411, "length_required")
                return None
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self._reply(400, "invalid_length")
                return None
            if len(lengths[0]) > 5:
                self._reply(413, "body_too_large")
                return None
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                self._reply(413, "body_too_large")
                return None
            if length == 0:
                self._reply(400, "invalid_json")
                return None
            media_type = self.headers.get("Content-Type", "").lower().replace(" ", "")
            if media_type not in ("application/json", "application/json;charset=utf-8"):
                self._reply(415, "unsupported_media_type")
                return None
            try:
                self.connection.settimeout(5)
                data = self.rfile.read(length)
            except OSError:
                self.close_connection = True
                return None
            if len(data) != length:
                self._reply(400, "incomplete_body")
                return None
            return data

        def do_POST(self) -> None:
            claim_prefix = TASK_PATH + "/"
            claim_suffix = "/claim"
            if self.path.startswith(claim_prefix) and self.path.endswith(claim_suffix):
                task_id = self.path[len(claim_prefix):-len(claim_suffix)]
                if not _TASK_ID.fullmatch(task_id):
                    self._reply(404, "not_found")
                    return
                data = self._read_body()
                if data is None:
                    return
                try:
                    worker_id, workspace_name = parse_assignment_request(data)
                except InvalidJSON:
                    self._reply(400, "invalid_json")
                    return
                except InvalidAssignment:
                    self._reply(422, "invalid_assignment")
                    return
                try:
                    assigned = inbox.claim_task(task_id, worker_id, workspace_name, scope)
                except TaskNotFound:
                    self._reply(404, "not_found")
                    return
                except AlreadyAssigned:
                    self._reply(409, "already_assigned")
                    return
                except InvalidAssignment:
                    self._reply(422, "invalid_workspace")
                    return
                except StorageUnavailable:
                    self._reply(503, "storage_unavailable")
                    return
                self._reply(201, "assigned", **assigned.metadata())
                return
            if self.path != TASK_PATH:
                self._reply(404, "not_found")
                return
            data = self._read_body()
            if data is None:
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
            except StorageUnavailable:
                self._reply(503, "storage_unavailable")
                return
            status = "accepted_durable" if inbox.durable else "accepted_in_memory"
            self._reply(202, status, receipt=record.receipt,
                        **record.metadata(), priority=record.submission.priority)

        def do_GET(self) -> None:
            if self.path == TASK_PATH:
                self._reply(405, "method_not_allowed")
                return
            if self.path.startswith("/v1/projects/"):
                for suffix in ("/task-order", "/next-task"):
                    if not self.path.endswith(suffix):
                        continue
                    project_id = self.path[len("/v1/projects/"):-len(suffix)]
                    if not _PROJECT_ID.fullmatch(project_id):
                        self._reply(404, "not_found")
                        return
                    try:
                        if suffix == "/task-order":
                            ordered = inbox.topological_order(project_id, scope)
                        else:
                            selected = inbox.select_next(project_id, scope)
                    except ProjectForbidden:
                        self._reply(403, "project_forbidden")
                        return
                    if suffix == "/task-order":
                        self._reply(200, "ordered", project_id=project_id,
                                    task_ids=ordered)
                    elif selected is None:
                        self._reply(200, "no_ready_task", project_id=project_id,
                                    task_id=None)
                    else:
                        self._reply(200, "selected", project_id=project_id,
                                    task_id=selected.task_id,
                                    priority=selected.priority)
                    return
                self._reply(404, "not_found")
                return
            if not self.path.startswith(TASK_PATH + "/"):
                self._reply(404, "not_found")
                return
            if self.path.endswith("/claim"):
                task_id = self.path[len(TASK_PATH) + 1:-len("/claim")]
                assignment = inbox.get_assignment(task_id, scope)
                if assignment is None:
                    self._reply(404, "not_found")
                    return
                self._reply(200, "assigned", **assignment.metadata())
                return
            if self.path.endswith("/state"):
                task_id = self.path[len(TASK_PATH) + 1:-len("/state")]
                state = inbox.state(task_id, scope)
                if state is None:
                    self._reply(404, "not_found")
                    return
                self._reply(200, "state_found", task_id=task_id,
                            task_state=state.value)
                return
            task_id = self.path[len(TASK_PATH) + 1:]
            record = inbox.get(task_id, scope)
            if record is None:
                self._reply(404, "not_found")
                return
            self._reply(200, "found", **record.metadata(),
                        depends_on=inbox.dependencies(task_id, scope),
                        priority=inbox.priority(task_id, scope))

        def do_PUT(self) -> None:
            prefix = TASK_PATH + "/"
            if not self.path.startswith(prefix):
                self._reply(405, "method_not_allowed")
                return
            suffix = next((item for item in ("/dependencies", "/priority")
                           if self.path.endswith(item)), None)
            if suffix is None:
                self._reply(405, "method_not_allowed")
                return
            task_id = self.path[len(prefix):-len(suffix)]
            if not _TASK_ID.fullmatch(task_id):
                self._reply(404, "not_found")
                return
            data = self._read_body()
            if data is None:
                return
            if suffix == "/priority":
                try:
                    requested_priority = parse_priority_update(data)
                except InvalidJSON:
                    self._reply(400, "invalid_json")
                    return
                except InvalidTask:
                    self._reply(422, "invalid_priority")
                    return
                try:
                    priority = inbox.set_priority(task_id, requested_priority, scope)
                except TaskNotFound:
                    self._reply(404, "not_found")
                    return
                except StorageUnavailable:
                    self._reply(503, "storage_unavailable")
                    return
                self._reply(200, "priority_updated", task_id=task_id,
                            priority=priority)
                return
            try:
                prerequisites = parse_dependency_update(data)
            except InvalidJSON:
                self._reply(400, "invalid_json")
                return
            except InvalidTask:
                self._reply(422, "invalid_dependencies")
                return
            try:
                depends_on = inbox.set_dependencies(task_id, prerequisites, scope)
            except TaskNotFound:
                self._reply(404, "not_found")
                return
            except DependencyNotFound:
                self._reply(404, "dependency_not_found")
                return
            except DependencyCycle:
                self._reply(409, "dependency_cycle")
                return
            except StorageUnavailable:
                self._reply(503, "storage_unavailable")
                return
            self._reply(200, "dependencies_updated", task_id=task_id,
                        depends_on=depends_on)

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
    parser.add_argument("--db", help="private SQLite inbox file under a 0700 directory")
    parser.add_argument("--workspace-root", help="trusted private root of pre-existing workspaces")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    try:
        scope = OwnerScope(args.owner_id, frozenset(args.project_id))
    except ValueError as exc:
        parser.error(str(exc))
    store = None
    try:
        workspace_root = (WorkspaceRoot(args.workspace_root)
                          if args.workspace_root is not None else None)
        if args.db is not None:
            store = SQLiteTaskStore(args.db)
        inbox = TaskInbox(store=store, workspace_root=workspace_root)
    except (StorageUnavailable, InvalidAssignment) as exc:
        if store is not None:
            store.close()
        parser.error(str(exc))
    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port),
                                 task_handler(inbox, scope)) as server:
            mode = "SQLite durable" if store is not None else "volatile RAM"
            print(f"task intake listening on 127.0.0.1:{args.port} ({mode}, no client auth)")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        inbox.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
