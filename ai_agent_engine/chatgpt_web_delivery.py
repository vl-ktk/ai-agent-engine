"""WEB-13: metadata-only, crash-conservative mock Web task delivery.

An existing private TaskDeliveryManager owns durable attempt tokens and ACKs.
This optional, distinct SQLite journal pins Web-specific correlation/lease and
response hashes without copying any prompts, answers, grants or credentials.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import Lock
import time

from .chatgpt_web_core import (
    WebCoreTaskAdapter, WebCoreTaskOutcome,
)
from .chatgpt_web_failures import WebFaultGuard
from .chatgpt_web_rotation import WebRotationReceipt
from .task_api import OwnerScope, _TASK_ID
from .task_delivery import TaskDeliveryManager
from .task_response import TaskExecutionResponse


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_APP_ID = 0x4157444C  # AWDL, WEB-13 private sidecar; never the engine DB.


class WebDeliveryHold(RuntimeError):
    """Sanitized denial, including uncertain external/browser outcomes."""


@dataclass(frozen=True, slots=True)
class WebDeliverySnapshot:
    correlation_id: str
    task_id: str
    attempt: int
    state: str
    lease_expires_at: float
    retry_at: float
    response_sha256: str | None


@dataclass(frozen=True, slots=True)
class WebDispatchResult:
    receipt: WebDeliverySnapshot
    executed: bool
    reason: str
    response: TaskExecutionResponse | None


def _correlation(owner_id: str, project_id: str, agent_id: str,
                 task_id: str) -> str:
    blob = json.dumps(("web13-v1", owner_id, project_id, agent_id, task_id),
                      separators=(",", ":")).encode("utf-8")
    return "web13_" + sha256(blob).hexdigest()[:32]


class SQLiteWebDeliveryJournal:
    """Exclusive, private opt-in SQLite metadata journal; no task DB schema change.

    A lease expiry means HOLD for review, never authorization for a second
    browser send. A prior active/verified attempt cannot be superseded.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        self._lock = Lock()
        self._fd: int | None = None
        try:
            parent = os.lstat(self.path.parent)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise WebDeliveryHold("private delivery directory required")
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR |
                               getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(self._fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise WebDeliveryHold("private delivery file required")
            import fcntl  # Linux-only, like the existing TaskInbox.
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with closing(sqlite3.connect(self.path, timeout=8)) as db, db:
                db.execute("PRAGMA synchronous=FULL")
                app = db.execute("PRAGMA application_id").fetchone()[0]
                ver = db.execute("PRAGMA user_version").fetchone()[0]
                if (app, ver) == (0, 0):
                    if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise WebDeliveryHold("unknown delivery file")
                    db.execute("""CREATE TABLE web13_delivery (
                        task TEXT PRIMARY KEY, owner TEXT NOT NULL,
                        project TEXT NOT NULL, agent TEXT NOT NULL,
                        correlation TEXT NOT NULL UNIQUE,
                        request_sha TEXT NOT NULL, token_sha TEXT NOT NULL,
                        attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 5),
                        state TEXT NOT NULL CHECK(state IN
                            ('ACTIVE','DEFERRED','EXHAUSTED','VERIFIED','ACKED')),
                        lease_until REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                        response_sha TEXT, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL)""")
                    db.execute(f"PRAGMA application_id={_APP_ID}")
                    db.execute("PRAGMA user_version=1")
                elif (app, ver) != (_APP_ID, 1):
                    raise WebDeliveryHold("unknown delivery journal version")
                if (db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                        or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
                    raise WebDeliveryHold("delivery journal integrity failed")
        except Exception:
            self.close()
            raise WebDeliveryHold("delivery journal unavailable") from None

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    @contextmanager
    def _connect(self):
        if self._fd is None:
            raise WebDeliveryHold("delivery journal closed")
        db = sqlite3.connect(self.path, timeout=8)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=8000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _scope(owner: str, project: str, agent: str, task: str) -> None:
        if (any(type(x) is not str or not _IDENTIFIER.fullmatch(x)
                for x in (owner, project, agent))
                or type(task) is not str or not _TASK_ID.fullmatch(task)):
            raise WebDeliveryHold("invalid web delivery scope")

    @staticmethod
    def _snapshot(row: tuple) -> WebDeliverySnapshot:
        return WebDeliverySnapshot(row[0], row[1], row[2], row[3],
                                   row[4], row[5], row[6])

    def snapshot(self, *, owner_id: str, project_id: str,
                 agent_id: str, task_id: str) -> WebDeliverySnapshot | None:
        self._scope(owner_id, project_id, agent_id, task_id)
        with self._lock, self._connect() as db:
            row = db.execute("""SELECT correlation,task,attempt,state,lease_until,
                retry_at,response_sha FROM web13_delivery
                WHERE owner=? AND project=? AND agent=? AND task=?""",
                (owner_id, project_id, agent_id, task_id)).fetchone()
        return None if row is None else self._snapshot(row)

    def begin(self, *, owner_id: str, project_id: str, agent_id: str,
              task_id: str, request_sha256: str, attempt: int,
              token_sha256: str, lease_expires_at: float, now: float
              ) -> WebDeliverySnapshot:
        self._scope(owner_id, project_id, agent_id, task_id)
        if (type(request_sha256) is not str or not _HASH.fullmatch(request_sha256)
                or type(token_sha256) is not str or not _HASH.fullmatch(token_sha256)
                or type(attempt) is not int or not 1 <= attempt <= 5
                or type(now) not in (int, float) or not 0 < now < 1e12
                or type(lease_expires_at) not in (int, float)
                or not now < lease_expires_at <= now + 3600):
            raise WebDeliveryHold("invalid delivery lease")
        correlation = _correlation(owner_id, project_id, agent_id, task_id)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT owner,project,agent,correlation,
                request_sha,attempt,state,retry_at FROM web13_delivery
                WHERE task=?""", (task_id,)).fetchone()
            if previous is None:
                db.execute("""INSERT INTO web13_delivery VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, owner_id, project_id, agent_id, correlation,
                     request_sha256, token_sha256, attempt, "ACTIVE",
                     lease_expires_at, 0.0, None, now, now))
            else:
                if (previous[:5] != (owner_id, project_id, agent_id,
                                     correlation, request_sha256)
                        or previous[6] != "DEFERRED"
                        or previous[5] + 1 != attempt
                        or now < previous[7]):
                    raise WebDeliveryHold("delivery attempt already active or mismatched")
                db.execute("""UPDATE web13_delivery SET token_sha=?,attempt=?,
                    state='ACTIVE',lease_until=?,retry_at=0,response_sha=NULL,
                    updated_at=? WHERE task=?""",
                    (token_sha256, attempt, lease_expires_at, now, task_id))
        return WebDeliverySnapshot(correlation, task_id, attempt,
                                   "ACTIVE", lease_expires_at, 0.0, None)

    def _transition(self, *, owner: str, project: str, agent: str, task: str,
                    attempt: int, token_sha: str, expected: str, target: str,
                    retry_at: float = 0.0, response_sha: str | None = None) -> None:
        self._scope(owner, project, agent, task)
        if (type(token_sha) is not str or not _HASH.fullmatch(token_sha)
                or type(attempt) is not int or attempt < 1
                or (response_sha is not None and not _HASH.fullmatch(response_sha))):
            raise WebDeliveryHold("unverified delivery receipt")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("""UPDATE web13_delivery SET state=?,retry_at=?,
                response_sha=?,updated_at=? WHERE owner=? AND project=?
                AND agent=? AND task=? AND attempt=? AND token_sha=? AND state=?""",
                (target, retry_at, response_sha, time.time(), owner, project,
                 agent, task, attempt, token_sha, expected)).rowcount
            if count != 1:
                raise WebDeliveryHold("delivery transition unverified")

    def defer(self, *, owner: str, project: str, agent: str, task: str,
              attempt: int, token_sha: str, status: str,
              retry_at: float) -> None:
        if status not in ("retryable", "exhausted"):
            raise WebDeliveryHold("invalid safe retry state")
        self._transition(owner=owner, project=project, agent=agent, task=task,
                         attempt=attempt, token_sha=token_sha,
                         expected="ACTIVE",
                         target="DEFERRED" if status == "retryable" else "EXHAUSTED",
                         retry_at=retry_at)

    def verified(self, *, owner: str, project: str, agent: str, task: str,
                 attempt: int, token_sha: str, response_sha: str) -> None:
        self._transition(owner=owner, project=project, agent=agent, task=task,
                         attempt=attempt, token_sha=token_sha,
                         expected="ACTIVE", target="VERIFIED",
                         response_sha=response_sha)

    def ack(self, *, owner: str, project: str, agent: str, task: str,
            attempt: int, token_sha: str, response_sha: str) -> None:
        self._transition(owner=owner, project=project, agent=agent, task=task,
                         attempt=attempt, token_sha=token_sha,
                         expected="VERIFIED", target="ACKED",
                         response_sha=response_sha)


class WebIdempotentDispatcher:
    """No second browser side effect after an issued durable delivery token.

    Only capacity denials are conclusively pre-send and retryable (1..5,
    bounded backoff). An ACTIVE/VERIFIED token is never reissued, including
    after lease expiry or restart. WEB-14's opt-in bound fault guard refuses
    new tickets for agents whose BrowserPort reported an unsafe outcome;
    WEB-15 owns deeper durable agent/session recovery.
    """

    def __init__(self, core: WebCoreTaskAdapter,
                 delivery: TaskDeliveryManager,
                 journal: SQLiteWebDeliveryJournal, *,
                 max_attempts: int = 3, lease_seconds: int = 120,
                 clock=time.time, fault_guard: WebFaultGuard | None = None):
        if (not isinstance(core, WebCoreTaskAdapter)
                or not isinstance(delivery, TaskDeliveryManager)
                or delivery.inbox is not core.inbox
                or not isinstance(journal, SQLiteWebDeliveryJournal)
                or type(max_attempts) is not int or not 1 <= max_attempts <= 5
                or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600
                or not callable(clock)
                or (fault_guard is not None
                    and not isinstance(fault_guard, WebFaultGuard))):
            raise ValueError("trusted bounded web delivery dependencies required")
        self.core = core
        self.delivery = delivery
        self.journal = journal
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self._clock = clock
        self.fault_guard = fault_guard

    def dispatch(self, raw_request: bytes, *, task_id: str, worker_id: str,
                 requested_model_id: str | None = None,
                 requested_reasoning_effort: str | None = None,
                 handoff_receipt: WebRotationReceipt | None = None
                 ) -> WebDispatchResult:
        scope: OwnerScope = self.core.scope
        try:
            record, request, _pin, _model = self.core.verify_binding(
                raw_request, task_id=task_id, worker_id=worker_id,
                requested_model_id=requested_model_id,
                handoff_receipt=handoff_receipt)
            if self.fault_guard is not None:
                # The same guard must wrap the actual scoped BrowserPort;
                # a detached dummy guard must not claim WEB-14 protection.
                slot = self.core.sessions._slot(
                    owner_id=scope.owner_id, project_id=record.project_id,
                    agent_id=worker_id)
                self.fault_guard.require_bound_port(slot.browser)
                self.fault_guard.require_ready(
                    owner_id=scope.owner_id, project_id=record.project_id,
                    agent_id=worker_id)
            # TaskDeliveryManager is the existing atomic v17 delivery ledger.
            ticket = self.delivery.reserve(
                task_id, scope, actor_id=worker_id,
                max_attempts=self.max_attempts)
            if not ticket.should_execute:
                snap = self.journal.snapshot(
                    owner_id=scope.owner_id, project_id=record.project_id,
                    agent_id=worker_id, task_id=task_id)
                if snap is None:
                    raise WebDeliveryHold("active delivery has no matching journal")
                return WebDispatchResult(snap, False, "already_reserved", None)
            if ticket.token is None:
                raise WebDeliveryHold("missing durable attempt token")
            token_sha = sha256(ticket.token.encode("ascii")).hexdigest()
            request_sha = sha256(request.to_bytes()).hexdigest()
            now = self._clock()
            snap = self.journal.begin(
                owner_id=scope.owner_id, project_id=record.project_id,
                agent_id=worker_id, task_id=task_id,
                request_sha256=request_sha, attempt=ticket.attempt,
                token_sha256=token_sha, lease_expires_at=now+self.lease_seconds,
                now=now)
            if self._clock() >= snap.lease_expires_at:
                raise WebDeliveryHold("delivery lease expired before send")
            coords = dict(owner=scope.owner_id, project=record.project_id,
                          agent=worker_id, task=task_id,
                          attempt=ticket.attempt, token_sha=token_sha)
            def record_verified(response: TaskExecutionResponse) -> None:
                self.journal.verified(**coords,
                    response_sha=sha256(response.to_bytes()).hexdigest())
            outcome: WebCoreTaskOutcome = self.core.execute(
                raw_request, task_id=task_id, worker_id=worker_id,
                requested_model_id=requested_model_id,
                requested_reasoning_effort=requested_reasoning_effort,
                handoff_receipt=handoff_receipt,
                response_verified=record_verified)
            if not outcome.admitted:
                # No browser call occurred; this is the only safe retry path.
                settled = self.delivery.report(task_id, scope,
                    actor_id=worker_id, token=ticket.token, success=False)
                self.journal.defer(**coords, status=settled.status,
                                   retry_at=settled.retry_at)
                latest = self.journal.snapshot(
                    owner_id=scope.owner_id, project_id=record.project_id,
                    agent_id=worker_id, task_id=task_id)
                return WebDispatchResult(latest, False,
                                          "capacity_" + outcome.reason, None)
            if (outcome.response is None
                    or outcome.response.task_id != task_id
                    or outcome.response.provider != "chatgpt_web"):
                raise WebDeliveryHold("unverified completed web delivery")
            settled = self.delivery.report(task_id, scope,
                actor_id=worker_id, token=ticket.token, success=True)
            if settled.status != "completed":
                raise WebDeliveryHold("web delivery ACK unavailable")
            self.journal.ack(**coords,
                response_sha=sha256(outcome.response.to_bytes()).hexdigest())
            latest = self.journal.snapshot(
                owner_id=scope.owner_id, project_id=record.project_id,
                agent_id=worker_id, task_id=task_id)
            return WebDispatchResult(latest, True, "completed", outcome.response)
        except Exception:
            # Unknown effects or gaps between SQLite writes are never retried.
            raise WebDeliveryHold("web delivery held for reconciliation") from None
