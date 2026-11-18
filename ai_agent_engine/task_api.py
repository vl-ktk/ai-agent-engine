"""Stages 5.1-5.12: scoped intake, durable ordering and audited task lifecycle.

Owner identity is bound by trusted server configuration, NOT HTTP input.
Use --db for SQLite persistence; without it the inbox stays in memory.
This is not client authentication, execution or worker scheduling.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Lock
from typing import Type
from uuid import uuid4

from .task_assignment import (
    AlreadyAssigned, InvalidAssignment, TaskAssignment, WorkspaceBusy,
    WorkspaceLease, WorkspaceRoot,
    validate_worker, validate_workspace_name,
)
from .task_dag import DependencyCycle, TaskDAG
from .task_state import (
    TERMINAL_STATES, TaskState, TaskTransitionEvent, TransitionRejected,
    can_transition,
)
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
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class InvalidJSON(ValueError):
    """Malformed, duplicated or non-UTF-8 JSON input."""


class InvalidTask(ValueError):
    """Task fields do not match the narrow admission contract."""


class IdempotencyConflict(ValueError):
    """An existing key is bound to a different task payload."""


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
        self._leases: dict[str, WorkspaceLease] = {}
        self._states: dict[str, TaskState] = {}
        self._idempotent: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._events: list[TaskTransitionEvent] = []  # RAM-only audit without SQLite
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
                self._release_leases()
                store.close()
                raise

    def _release_leases(self) -> None:
        """Call only under inbox lock or before the inbox is published."""
        for lease in self._leases.values():
            lease.close()
        self._leases.clear()

    def _restore(self) -> None:
        """Fail closed on invalid persisted owners, edges or cycles."""
        if self._store is None:
            return
        try:
            rows, edges, claims, states = self._store.load()
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
            for task_id, stored_state in states:
                if task_id not in self._by_id or task_id in self._states:
                    raise StorageUnavailable("invalid persisted task state target")
                self._states[task_id] = TaskState(stored_state)
            if set(self._states) != set(self._by_id):
                raise StorageUnavailable("missing persisted task state")
            for task_id, state in self._states.items():
                has_claim = task_id in self._assignments
                if ((state is TaskState.QUEUED and has_claim)
                        or (state not in (TaskState.QUEUED, TaskState.CANCELLED)
                            and not has_claim)):
                    raise StorageUnavailable("persisted claim and task state disagree")
            for task_id, assignment in self._assignments.items():
                if self._states[task_id] in TERMINAL_STATES:
                    continue  # finished workspaces can be claimed by other tasks
                if self._workspace_root is None:
                    raise StorageUnavailable("active assignments need workspace root")
                try:
                    lease = self._workspace_root.acquire(assignment.workspace_name)
                except (WorkspaceBusy, InvalidAssignment) as exc:
                    raise StorageUnavailable("persisted workspace unavailable") from exc
                if lease.directory != assignment.workspace_dir:
                    lease.close()
                    raise StorageUnavailable("persisted workspace changed")
                self._leases[task_id] = lease
        except (ValueError, TypeError, UnicodeError, OverflowError,
                DependencyCycle, InvalidJSON, InvalidTask) as exc:
            raise StorageUnavailable("invalid persisted task graph") from exc

    def accept(self, submission: TaskSubmission, scope: OwnerScope,
               *, idempotency_key: str | None = None) -> TaskRecord:
        """Admit a task once per trusted owner/project/key and exact payload.

        With no key this retains the original 5.1 API behaviour. Replays of
        a keyed admission return the same task ID, even when the inbox is full
        or the task has completed. No raw key or prompt goes into the ledger.
        """
        if not isinstance(submission, TaskSubmission):
            raise TypeError("expected a validated TaskSubmission")
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(submission.priority) is not int or not 0 <= submission.priority <= MAX_PRIORITY:
            raise InvalidTask("invalid priority")
        if idempotency_key is not None and (type(idempotency_key) is not str
                                             or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key)):
            raise InvalidTask("invalid idempotency key")
        with self._lock:
            if submission.project_id not in scope.project_ids:
                raise ProjectForbidden("project outside configured scope")
            key_ref = None
            fingerprint = None
            if idempotency_key is not None:
                digest = sha256(idempotency_key.encode("ascii")).hexdigest()
                key_ref = (scope.owner_id, submission.project_id, digest)
                fingerprint = sha256(json.dumps({
                    "project_id": submission.project_id,
                    "title": submission.title,
                    "prompt": submission.prompt,
                    "parent_task_id": submission.parent_task_id,
                    "priority": submission.priority,
                }, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest()
                existing = (self._store.admission_key(*key_ref)
                            if self._store is not None
                            else self._idempotent.get(key_ref))
                if existing is not None:
                    previous_id, previous_fingerprint = existing
                    if fingerprint != previous_fingerprint:
                        raise IdempotencyConflict("idempotency key reused for different task")
                    if previous_id not in self._by_id:
                        raise StorageUnavailable("idempotent task record is missing")
                    return self._by_id[previous_id]
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
                self._store.insert_task(record,
                    key_hash=key_ref[2] if key_ref else None,
                    payload_hash=fingerprint)  # atomic task + key before ACK
            self._next_receipt += 1
            self._pending.append(record)
            self._by_id[task_id] = record
            self._dag.add_task(task_id, receipt)
            self._priorities[task_id] = submission.priority
            self._states[task_id] = TaskState.QUEUED
            if key_ref is not None:
                self._idempotent[key_ref] = (task_id, fingerprint)
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
                self._reject_transition(task_id, self._states[task_id],
                                        TaskState.ASSIGNED, worker_id,
                                        "already_assigned")
                raise AlreadyAssigned("task already has a worker and workspace")
            if self._states[task_id] is not TaskState.QUEUED:
                self._reject_transition(task_id, self._states[task_id],
                                        TaskState.ASSIGNED, worker_id, "not_queued")
                raise TransitionRejected("not_queued")
            if self._workspace_root is None:
                raise InvalidAssignment("trusted workspace root not configured")
            try:
                lease = self._workspace_root.acquire(workspace_name)
            except WorkspaceBusy:
                self._reject_transition(task_id, self._states[task_id],
                                        TaskState.ASSIGNED, worker_id,
                                        "workspace_busy")
                raise
            workspace_dir = lease.directory
            assignment = TaskAssignment(task_id, worker_id, workspace_name,
                                        workspace_dir)
            try:
                if self._store is not None:
                    self._store.insert_assignment(assignment)
            except BaseException:
                lease.close()
                raise
            self._assignments[task_id] = assignment
            self._leases[task_id] = lease
            self._states[task_id] = TaskState.ASSIGNED
            if self._store is None:
                self._record_volatile(task_id, TaskState.QUEUED,
                                      TaskState.ASSIGNED, worker_id, True, "claim")
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
        """Read committed lifecycle state without exposing another owner."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            return None
        with self._lock:
            try:
                self._scoped_record(task_id, scope)
            except TaskNotFound:
                return None
            return self._states[task_id]

    def _record_volatile(self, task_id: str, previous: TaskState,
                         requested: TaskState, actor_id: str, accepted: bool,
                         reason_code: str) -> None:
        """Call with inbox lock; memory-only audit when --db is omitted."""
        self._events.append(TaskTransitionEvent(
            len(self._events) + 1, task_id, previous, requested, actor_id,
            accepted, reason_code, datetime.now(timezone.utc).isoformat()))

    def _reject_transition(self, task_id: str, previous: TaskState,
                           requested: TaskState, actor_id: str,
                           reason_code: str) -> None:
        """Commit denial evidence before returning a transition rejection."""
        if self._store is not None:
            self._store.record_rejected_transition(
                task_id, previous.value, requested.value, actor_id, reason_code)
        else:
            self._record_volatile(task_id, previous, requested, actor_id,
                                  False, reason_code)

    def transition_state(self, task_id: str, target: TaskState, scope: OwnerScope,
                         *, actor_id: str) -> TaskState:
        """Trusted internal transition; never callable from HTTP.

        The state update and sanitized audit commit atomically in SQLite.
        This does not start or stop a worker; execution is a later stage.
        """
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if not isinstance(target, TaskState):
            raise TypeError("expected TaskState")
        validate_worker(actor_id)
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            raise TaskNotFound("invalid task identifier")
        with self._lock:
            self._scoped_record(task_id, scope)
            previous = self._states[task_id]
            if not can_transition(previous, target):
                reason_code = "invalid_edge"
            elif target is TaskState.ASSIGNED:
                # Generic state writes may not bypass the immutable claim.
                reason_code = "claim_required"
            elif (target in (TaskState.RUNNING, TaskState.SUCCEEDED, TaskState.FAILED)
                  and task_id in self._assignments
                  and self._assignments[task_id].worker_id != actor_id):
                reason_code = "worker_mismatch"
            else:
                reason_code = ""
            if reason_code:
                self._reject_transition(task_id, previous, target, actor_id,
                                        reason_code)
                raise TransitionRejected(reason_code)
            if self._store is not None:
                self._store.write_transition(task_id, previous.value,
                                             target.value, actor_id)
            self._states[task_id] = target
            if self._store is None:
                self._record_volatile(task_id, previous, target, actor_id,
                                      True, "transition")
            if target in TERMINAL_STATES:
                lease = self._leases.pop(task_id, None)
                if lease is not None:
                    lease.close()
            return target

    def transition_history(self, task_id: str, scope: OwnerScope) -> tuple[TaskTransitionEvent, ...]:
        """Trusted scoped read of metadata-only accepted and denied attempts."""
        if not isinstance(scope, OwnerScope):
            raise TypeError("expected a trusted OwnerScope")
        if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
            raise TaskNotFound("invalid task identifier")
        with self._lock:
            self._scoped_record(task_id, scope)
            if self._store is None:
                return tuple(event for event in self._events if event.task_id == task_id)
            return tuple(TaskTransitionEvent(
                event_id, saved_task_id, TaskState(previous), TaskState(target),
                actor_id, bool(accepted), reason_code, occurred_at)
                for (event_id, saved_task_id, previous, target, actor_id,
                     accepted, reason_code, occurred_at)
                in self._store.transition_history(task_id))

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
        """Release workspace and DB locks; never delete stored data."""
        with self._lock:
            try:
                if self._store is not None:
                    self._store.close()
            finally:
                self._release_leases()

    @property
    def durable(self) -> bool:
        return self._store is not None

    def select_next(self, project_id: str, scope: OwnerScope,
                    *, completed_task_ids: tuple[str, ...] = ()) -> TaskChoice | None:
        """Pure selection: eligible unfinished task with highest priority.

        A trusted scheduler may still supply completed IDs. Persisted
        SUCCEEDED states also unblock prerequisites. HTTP never accepts
        client-claimed completion. Selection is observation, not reservation.
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
            completed.update(task_id for task_id, state in self._states.items()
                             if state is TaskState.SUCCEEDED)
            ready = [record for record in self._pending
                     if record.owner_id == scope.owner_id
                     and record.project_id == project_id
                     and record.task_id not in completed
                     and self._states[record.task_id] is TaskState.QUEUED
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
                except WorkspaceBusy:
                    self._reply(409, "workspace_busy")
                    return
                except TransitionRejected:
                    self._reply(409, "task_not_claimable")
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
            keys = self.headers.get_all("Idempotency-Key") or []
            if len(keys) > 1:
                self._reply(422, "invalid_idempotency_key")
                return
            key = keys[0] if keys else None
            try:
                record = inbox.accept(submission, scope, idempotency_key=key)
            except IdempotencyConflict:
                self._reply(409, "idempotency_conflict")
                return
            except InvalidTask:
                self._reply(422, "invalid_idempotency_key")
                return
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
