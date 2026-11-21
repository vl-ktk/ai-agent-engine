"""WEB-15: private, fail-stop Web queue and cold-start reconciliation.

No browser, credential, prompt or answer is persisted here. TaskInbox holds
the canonical task; this journal holds only a scoped pointer and checkpoint
digest. Unknown effects are never replayed, including after process death.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import Lock
import time

from .chatgpt_web_delivery import WebIdempotentDispatcher
from .chatgpt_web_failures import WebFaultCode, WebFaultGuard
from .chatgpt_web_handoff import (
    ScopedCheckpointSnapshot, checkpoint_sha256, _safe_text,
)
from .task_api import _TASK_ID
from .task_permissions import ProtectedAction
from .task_request import build_task_request
from .task_state import TaskState


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_APP_ID = 0x41575243  # AWRC: independent private WEB-15 journal.


class WebRecoveryHold(RuntimeError):
    """Sanitized: manually reconcile before any further external send."""


@dataclass(frozen=True, slots=True)
class WebRecoveryEntry:
    task_id: str
    project_id: str
    agent_id: str
    checkpoint_id: str
    state: str
    reason: str


class SQLiteWebRecoveryQueue:
    """Exclusive metadata-only SQLite journal; TaskInbox v17 is untouched."""

    def __init__(self, path: str | Path, *, max_pending: int = 128):
        if type(max_pending) is not int or not 1 <= max_pending <= 128:
            raise ValueError("bounded recovery queue required")
        self.path = Path(path).absolute()
        self.max_pending = max_pending
        self._lock = Lock()
        self._dispatch_locks: dict[tuple[str, str, str], Lock] = {}
        self._fd: int | None = None
        try:
            parent = os.lstat(self.path.parent)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise WebRecoveryHold("private recovery directory required")
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR |
                               getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(self._fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise WebRecoveryHold("private recovery file required")
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with closing(sqlite3.connect(self.path, timeout=8)) as db, db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA foreign_keys=ON")
                app, ver = (db.execute("PRAGMA application_id").fetchone()[0],
                            db.execute("PRAGMA user_version").fetchone()[0])
                if (app, ver) == (0, 0):
                    if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise WebRecoveryHold("foreign recovery database")
                    db.execute("""CREATE TABLE agents (
                        owner TEXT NOT NULL, project TEXT NOT NULL,
                        agent TEXT NOT NULL, chat_project TEXT NOT NULL,
                        profile TEXT NOT NULL, mailbox TEXT NOT NULL,
                        fault TEXT, PRIMARY KEY(owner,project,agent))""")
                    db.execute("""CREATE TABLE queue (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        task TEXT UNIQUE NOT NULL, owner TEXT NOT NULL,
                        project TEXT NOT NULL, agent TEXT NOT NULL,
                        checkpoint TEXT NOT NULL, revision INTEGER NOT NULL,
                        checkpoint_sha TEXT NOT NULL, request_sha TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN
                            ('PENDING','IN_FLIGHT','COMPLETED','HOLD')),
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(owner,project,agent) REFERENCES
                            agents(owner,project,agent))""")
                    db.execute(f"PRAGMA application_id={_APP_ID}")
                    db.execute("PRAGMA user_version=1")
                elif (app, ver) != (_APP_ID, 1):
                    raise WebRecoveryHold("recovery journal version unknown")
                if (db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                        or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
                    raise WebRecoveryHold("recovery journal damaged")
        except Exception:
            self.close()
            raise WebRecoveryHold("recovery journal unavailable") from None

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    @contextmanager
    def _db(self):
        if self._fd is None:
            raise WebRecoveryHold("recovery journal closed")
        with closing(sqlite3.connect(self.path, timeout=8)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    def enqueue(self, *, owner: str, project: str, agent: str,
                chat_project: str, profile: str, mailbox: str, task: str,
                checkpoint: str, revision: int, checkpoint_sha: str,
                request_sha: str) -> None:
        if (any(type(s) is not str or not _IDENTIFIER.fullmatch(s)
                for s in (owner, project, agent, chat_project, checkpoint))
                or type(task) is not str or not _TASK_ID.fullmatch(task)
                or any(type(s) is not str or not _HASH.fullmatch(s)
                       for s in (checkpoint_sha, request_sha))
                or type(profile) is not str or not profile.startswith("profile-")
                or type(mailbox) is not str or not mailbox.startswith("mailbox-")
                or type(revision) is not int or revision < 1):
            raise WebRecoveryHold("untrusted recovery metadata")
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT chat_project,profile,mailbox,fault FROM agents "
                                  "WHERE owner=? AND project=? AND agent=?",
                                  (owner, project, agent)).fetchone()
            if existing is None:
                db.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,NULL)",
                           (owner, project, agent, chat_project, profile, mailbox))
            elif existing != (chat_project, profile, mailbox, None):
                raise WebRecoveryHold("recovery session changed or held")
            previous = db.execute("""SELECT owner,project,agent,checkpoint,
                revision,checkpoint_sha,request_sha FROM queue WHERE task=?""",
                (task,)).fetchone()
            expected = (owner, project, agent, checkpoint, revision,
                        checkpoint_sha, request_sha)
            if previous is not None:
                if previous != expected:
                    raise WebRecoveryHold("recovery task was rebound")
                return
            if db.execute("SELECT COUNT(*) FROM queue WHERE state!='COMPLETED'").fetchone()[0] >= self.max_pending:
                raise WebRecoveryHold("recovery queue full")
            db.execute("""INSERT INTO queue(task,owner,project,agent,checkpoint,
                revision,checkpoint_sha,request_sha,state,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'PENDING',?)""",
                (task, owner, project, agent, checkpoint,
                 revision, checkpoint_sha, request_sha, time.time()))

    def first_pending(self, owner: str, project: str, agent: str) -> str | None:
        with self._lock, self._db() as db:
            row = db.execute("""SELECT task FROM queue WHERE owner=?
                AND project=? AND agent=? AND state='PENDING'
                ORDER BY seq LIMIT 1""", (owner, project, agent)).fetchone()
            return None if row is None else row[0]

    def agent_dispatch_lock(self, owner: str, project: str, agent: str) -> Lock:
        with self._lock:
            return self._dispatch_locks.setdefault((owner, project, agent), Lock())

    def entries(self, owner: str, *, project: str | None = None,
                agent: str | None = None) -> tuple[tuple, ...]:
        if (project is None) != (agent is None):
            raise WebRecoveryHold("recovery filter scope incomplete")
        with self._lock, self._db() as db:
            sql = """SELECT q.task,q.owner,q.project,q.agent,
                a.chat_project,a.profile,a.mailbox,a.fault,q.checkpoint,
                q.revision,q.checkpoint_sha,q.request_sha,q.state
                FROM queue q JOIN agents a ON a.owner=q.owner AND
                a.project=q.project AND a.agent=q.agent
                WHERE q.owner=?"""
            if project is not None:
                sql += " AND q.project=? AND q.agent=?"
            sql += " ORDER BY q.seq"
            return tuple(db.execute(sql, (owner,) if project is None else
                                    (owner, project, agent)))

    def transition(self, task: str, expected: str, target: str) -> None:
        if (expected, target) not in (
                ("PENDING", "IN_FLIGHT"), ("PENDING", "HOLD"),
                ("IN_FLIGHT", "PENDING"), ("IN_FLIGHT", "COMPLETED"),
                ("IN_FLIGHT", "HOLD")):
            raise WebRecoveryHold("invalid recovery transition")
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("UPDATE queue SET state=?,updated_at=? "
                               "WHERE task=? AND state=?",
                               (target, time.time(), task, expected)).rowcount
            if count != 1:
                raise WebRecoveryHold("recovery state changed")

    def hold_agent(self, *, owner: str, project: str, agent: str,
                   code: WebFaultCode) -> None:
        if type(code) is not WebFaultCode:
            raise WebRecoveryHold("invalid recovery fault")
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("UPDATE agents SET fault=COALESCE(fault,?) "
                               "WHERE owner=? AND project=? AND agent=?",
                               (code.value, owner, project, agent)).rowcount
            if count != 1:
                raise WebRecoveryHold("recovery agent unavailable")


class WebRecoveryCoordinator:
    """Explicit opt-in queue: no automatic run on restart or unknown send."""

    def __init__(self, dispatcher: WebIdempotentDispatcher,
                 queue: SQLiteWebRecoveryQueue, checkpoint_source: object):
        if (not isinstance(dispatcher, WebIdempotentDispatcher)
                or dispatcher.fault_guard is None
                or not isinstance(queue, SQLiteWebRecoveryQueue)
                or not callable(getattr(checkpoint_source, "read_checkpoint", None))):
            raise ValueError("trusted WEB-13/14 recovery dependencies required")
        self.dispatcher = dispatcher
        self.queue = queue
        self.checkpoints = checkpoint_source
        profile = dispatcher.fault_guard.profile
        dispatcher.fault_guard.attach_hold_sink(lambda code:
            queue.hold_agent(owner=profile.owner_id, project=profile.project_id,
                             agent=profile.agent_id, code=code))

    def _scope(self, worker: str):
        core = self.dispatcher.core
        profile = core.sessions.profile(owner_id=core.scope.owner_id,
                                        project_id=self.dispatcher.fault_guard.profile.project_id,
                                        agent_id=worker)
        binding = core.sessions.project_binding(owner_id=profile.owner_id,
            project_id=profile.project_id, agent_id=worker)
        guard = self.dispatcher.fault_guard
        guard.require_bound_port(core.sessions._slot(
            owner_id=profile.owner_id, project_id=profile.project_id,
            agent_id=worker).browser)
        return profile, binding

    def _checkpoint(self, worker: str, task: str, checkpoint: str,
                    revision: int, digest: str) -> tuple:
        core = self.dispatcher.core
        profile, binding = self._scope(worker)
        with core.sessions.scoped_project_operation(
                owner_id=profile.owner_id, project_id=profile.project_id,
                agent_id=worker):
            snapshot = self.checkpoints.read_checkpoint(
                binding=binding, profile=profile, task_id=task,
                checkpoint_id=checkpoint)
        if (type(snapshot) is not ScopedCheckpointSnapshot
                or tuple(getattr(snapshot, f) for f in (
                    "owner_id", "project_id", "agent_id", "chatgpt_project_id",
                    "profile_key", "mailbox_key", "task_id", "checkpoint_id")) !=
                (profile.owner_id, profile.project_id, profile.agent_id,
                 binding.chatgpt_project_id, profile.profile_key,
                 profile.mailbox_key, task, checkpoint)
                or snapshot.current is not True or snapshot.verified is not True
                or type(snapshot.revision) is not int or snapshot.revision != revision
                or snapshot.status != "in_progress"
                or not _safe_text(snapshot.summary, 2000)
                or not _safe_text(snapshot.next_action, 1000)
                or checkpoint_sha256(snapshot.status, snapshot.summary,
                                     snapshot.next_action) != digest):
            raise WebRecoveryHold("recovery checkpoint unverified")
        return profile, binding

    def enqueue(self, *, task_id: str, worker_id: str,
                checkpoint_id: str, revision: int, checkpoint_sha: str) -> None:
        try:
            core = self.dispatcher.core
            profile, binding = self._checkpoint(
                worker_id, task_id, checkpoint_id, revision, checkpoint_sha)
            self.dispatcher.fault_guard.require_ready(
                owner_id=profile.owner_id, project_id=profile.project_id,
                agent_id=worker_id)
            identity = core.sessions.identity(
                owner_id=profile.owner_id, project_id=profile.project_id,
                agent_id=worker_id)
            raw = build_task_request(core.inbox, core.scope, task_id,
                role=identity.role,
                required_actions=(ProtectedAction.EXTERNAL_REQUEST,)).to_bytes()
            core.verify_binding(raw, task_id=task_id, worker_id=worker_id)
            self.queue.enqueue(owner=profile.owner_id, project=profile.project_id,
                agent=worker_id, chat_project=binding.chatgpt_project_id,
                profile=profile.profile_key, mailbox=profile.mailbox_key,
                task=task_id, checkpoint=checkpoint_id, revision=revision,
                checkpoint_sha=checkpoint_sha,
                request_sha=sha256(raw).hexdigest())
        except Exception:
            raise WebRecoveryHold("recovery enqueue unverified") from None

    def inventory(self) -> tuple[WebRecoveryEntry, ...]:
        """Cold-start read/verification, never sends a message or retries work."""
        core = self.dispatcher.core
        found = []
        profile = self.dispatcher.fault_guard.profile
        for row in self.queue.entries(core.scope.owner_id,
                                      project=profile.project_id,
                                      agent=profile.agent_id):
            (task, owner, project, worker, chat_project, profile_key,
             mailbox, fault, checkpoint, revision, digest, request_sha,
             queued) = row
            reason = "unverified"
            try:
                profile, binding = self._checkpoint(
                    worker, task, checkpoint, revision, digest)
                if (owner, project, chat_project, profile_key, mailbox) != (
                        profile.owner_id, profile.project_id,
                        binding.chatgpt_project_id, profile.profile_key,
                        profile.mailbox_key):
                    raise WebRecoveryHold("session pin changed")
                identity = core.sessions.identity(owner_id=owner,
                    project_id=project, agent_id=worker)
                raw = build_task_request(core.inbox, core.scope, task,
                    role=identity.role,
                    required_actions=(ProtectedAction.EXTERNAL_REQUEST,)).to_bytes()
                if sha256(raw).hexdigest() != request_sha:
                    raise WebRecoveryHold("task changed")
                core.verify_binding(raw, task_id=task, worker_id=worker)
                task_state = core.inbox.state(task, core.scope)
                delivery = self.dispatcher.delivery.snapshot(task, core.scope)
                web = self.dispatcher.journal.snapshot(owner_id=owner,
                    project_id=project, agent_id=worker, task_id=task)
                if (task_state is TaskState.SUCCEEDED
                        and delivery is not None and delivery.status == "completed"
                        and web is not None and web.state == "ACKED"):
                    if queued == "IN_FLIGHT":
                        self.queue.transition(task, "IN_FLIGHT", "COMPLETED")
                    found.append(WebRecoveryEntry(task, project, worker,
                                                  checkpoint, "COMPLETED", "acked"))
                    continue
                if fault is not None:
                    code = WebFaultCode(fault)
                    self.dispatcher.fault_guard._hold(code)
                    raise WebRecoveryHold("agent previously held")
                # Only a never-sent PENDING task, or a proven pre-send
                # capacity denial, may be considered READY again.
                safe_first = (delivery is None and web is None)
                safe_defer = (delivery is not None and delivery.status == "retryable"
                              and web is not None and web.state == "DEFERRED")
                if (queued == "PENDING" and task_state is TaskState.ASSIGNED
                        and (safe_first or safe_defer)):
                    self.dispatcher.fault_guard.require_ready(
                        owner_id=owner, project_id=project, agent_id=worker)
                    if safe_defer and time.time() < max(delivery.retry_at, web.retry_at):
                        found.append(WebRecoveryEntry(task, project, worker,
                                                      checkpoint, "WAIT", "bounded_backoff"))
                        continue
                    found.append(WebRecoveryEntry(task, project, worker,
                                                  checkpoint, "READY", "verified_unsent"))
                    continue
                reason = "uncertain_delivery"
            except Exception:
                pass
            if queued != "HOLD" and queued != "COMPLETED":
                try:
                    self.queue.transition(task, queued, "HOLD")
                except Exception:
                    pass
            self.queue.hold_agent(owner=owner, project=project, agent=worker,
                                  code=WebFaultCode.UNKNOWN_SEND)
            self.dispatcher.fault_guard._hold(WebFaultCode.UNKNOWN_SEND)
            found.append(WebRecoveryEntry(task, project, worker,
                                          checkpoint, "HOLD", reason))
        return tuple(found)

    def dispatch_next(self, *, task_id: str, worker_id: str):
        """Explicit caller action, not a background replay or scheduler."""
        profile = self.dispatcher.fault_guard.profile
        with self.queue.agent_dispatch_lock(profile.owner_id, profile.project_id,
                                            profile.agent_id):
            return self._dispatch_next(task_id=task_id, worker_id=worker_id)

    def _dispatch_next(self, *, task_id: str, worker_id: str):
        core = self.dispatcher.core
        try:
            ready = self.inventory()
            if not any(e.task_id == task_id and e.agent_id == worker_id
                       and e.state == "READY" for e in ready):
                raise WebRecoveryHold("task not safe to dispatch")
            profile, _ = self._scope(worker_id)
            if self.queue.first_pending(profile.owner_id, profile.project_id,
                                        worker_id) != task_id:
                raise WebRecoveryHold("queue FIFO head mismatch")
            identity = core.sessions.identity(owner_id=profile.owner_id,
                project_id=profile.project_id, agent_id=worker_id)
            raw = build_task_request(core.inbox, core.scope, task_id,
                role=identity.role,
                required_actions=(ProtectedAction.EXTERNAL_REQUEST,)).to_bytes()
            self.queue.transition(task_id, "PENDING", "IN_FLIGHT")
            result = self.dispatcher.dispatch(raw, task_id=task_id,
                                              worker_id=worker_id)
            if result.executed and result.receipt.state == "ACKED":
                self.queue.transition(task_id, "IN_FLIGHT", "COMPLETED")
            elif result.receipt.state == "DEFERRED":
                self.queue.transition(task_id, "IN_FLIGHT", "PENDING")
            else:
                raise WebRecoveryHold("delivery needs review")
            return result
        except Exception:
            # Fail-stop after any uncertainty; the next inventory will HOLD.
            try:
                self.queue.transition(task_id, "IN_FLIGHT", "HOLD")
                profile = self.dispatcher.fault_guard.profile
                code = (self.dispatcher.fault_guard.snapshot().code
                        or WebFaultCode.UNKNOWN_SEND)
                self.queue.hold_agent(owner=profile.owner_id,
                                      project=profile.project_id,
                                      agent=profile.agent_id, code=code)
                self.dispatcher.fault_guard._hold(code)
            except Exception:
                pass
            raise WebRecoveryHold("web recovery dispatch held") from None
