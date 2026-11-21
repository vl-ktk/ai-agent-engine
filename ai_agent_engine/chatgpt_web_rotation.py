"""WEB-10: offline, idempotent ChatGPT Project mock chat rotation.

SQLite is only a small opt-in rotation test journal, not production TaskInbox
storage. A live browser is neither implemented nor authorized here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import re
import sqlite3
from threading import Lock
from typing import Iterator

from .chatgpt_web_chats import ProjectChatManager, ProjectChatRecord
from .chatgpt_web_context import ContextPressureDecision, WebContextMonitor
from .chatgpt_web_sessions import WebAgentScopeError, WebAgentSessionRegistry


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class WebChatRotationError(WebAgentScopeError):
    """Sanitized fail-closed rotation/journal refusal."""


@dataclass(frozen=True, slots=True)
class WebRotationReceipt:
    owner_id: str
    project_id: str
    agent_id: str
    chatgpt_project_id: str
    task_id: str
    checkpoint_id: str
    predecessor_id: str
    conversation_id: str
    rotation_id: str


def _rotation_id(*values: str) -> str:
    payload = b"".join(len(v.encode()).to_bytes(2, "big") + v.encode()
                       for v in values)
    return hashlib.sha256(b"web10-rotation-v1\0" + payload).hexdigest()


class SQLiteWebRotationJournal:
    """Durable opt-in mock journal; one SQLite file shared across restarts.

    Caller owns location, backup and access permissions; no production DB is
    opened by the library. Every mutation uses a bounded atomic transaction.
    """

    def __init__(self, path: str):
        if type(path) is not str or not path.strip() or path == ":memory:":
            raise ValueError("persistent test journal path required")
        self._path = path
        self._lock = Lock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS web10_chats (
                owner TEXT NOT NULL, project TEXT NOT NULL, agent TEXT NOT NULL,
                ordinal INTEGER NOT NULL, gpt_project TEXT NOT NULL,
                conversation TEXT NOT NULL UNIQUE, predecessor TEXT,
                successor TEXT, task TEXT, rotation TEXT,
                PRIMARY KEY (owner, project, agent, ordinal))""")
            db.execute("""CREATE TABLE IF NOT EXISTS web10_operations (
                owner TEXT NOT NULL, project TEXT NOT NULL, agent TEXT NOT NULL,
                task TEXT NOT NULL, checkpoint TEXT NOT NULL,
                predecessor TEXT NOT NULL, rotation TEXT NOT NULL UNIQUE,
                target TEXT, status TEXT NOT NULL CHECK(status IN ('PREPARED','COMMITTED')),
                PRIMARY KEY (owner, project, agent, task))""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=8)
        try:
            db.execute("PRAGMA busy_timeout=8000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _scope(owner_id: str, project_id: str, agent_id: str) -> tuple[str, str, str]:
        if any(type(x) is not str or not _ID.fullmatch(x)
               for x in (owner_id, project_id, agent_id)):
            raise WebChatRotationError("invalid rotation scope")
        return owner_id, project_id, agent_id

    def seed_initial(self, record: ProjectChatRecord) -> None:
        if (type(record) is not ProjectChatRecord
                or record.predecessor_id is not None
                or record.next_id is not None
                or record.task_id is not None or record.rotation_id is not None):
            raise WebChatRotationError("invalid initial chat for journal")
        scope = self._scope(record.owner_id, record.project_id, record.agent_id)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT conversation,gpt_project FROM web10_chats
                WHERE owner=? AND project=? AND agent=? ORDER BY ordinal""", scope).fetchall()
            if rows:
                if rows[0] != (record.conversation_id, record.chatgpt_project_id):
                    raise WebChatRotationError("journal initial chat mismatch")
                return
            try:
                db.execute("""INSERT INTO web10_chats VALUES
                    (?,?,?,?,?,?,?,?,?,?)""", (*scope, 0,
                    record.chatgpt_project_id, record.conversation_id,
                    None, None, None, None))
            except sqlite3.IntegrityError:
                raise WebChatRotationError("duplicate journal conversation") from None

    def history(self, *, owner_id: str, project_id: str,
                agent_id: str) -> tuple[ProjectChatRecord, ...]:
        scope = self._scope(owner_id, project_id, agent_id)
        with self._lock, self._connect() as db:
            rows = db.execute("""SELECT gpt_project, conversation, predecessor,
                successor, task, rotation FROM web10_chats
                WHERE owner=? AND project=? AND agent=? ORDER BY ordinal""", scope).fetchall()
        return tuple(ProjectChatRecord(*scope, *row) for row in rows)

    def prepare(self, *, owner_id: str, project_id: str, agent_id: str,
                task_id: str, checkpoint_id: str, predecessor_id: str,
                rotation_id: str) -> tuple[str, str | None]:
        scope = self._scope(owner_id, project_id, agent_id)
        if any(type(x) is not str or not _ID.fullmatch(x)
               for x in (task_id, checkpoint_id, predecessor_id, rotation_id)):
            raise WebChatRotationError("invalid rotation operation")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""SELECT checkpoint,predecessor,rotation,
                target,status FROM web10_operations
                WHERE owner=? AND project=? AND agent=? AND task=?""",
                (*scope, task_id)).fetchone()
            if existing:
                if existing[:3] != (checkpoint_id, predecessor_id, rotation_id):
                    raise WebChatRotationError("task rotation replay mismatch")
                return existing[4], existing[3]
            active = db.execute("""SELECT conversation FROM web10_chats
                WHERE owner=? AND project=? AND agent=?
                ORDER BY ordinal DESC LIMIT 1""", scope).fetchone()
            if active is None or active[0] != predecessor_id:
                raise WebChatRotationError("journal active chat changed")
            try:
                db.execute("""INSERT INTO web10_operations VALUES
                    (?,?,?,?,?,?,?,?,?)""", (*scope, task_id,
                    checkpoint_id, predecessor_id, rotation_id, None,
                    "PREPARED"))
            except sqlite3.IntegrityError:
                raise WebChatRotationError("rotation id collision") from None
            return "PREPARED", None

    def has_operation(self, *, owner_id: str, project_id: str, agent_id: str,
                      task_id: str, checkpoint_id: str,
                      predecessor_id: str, rotation_id: str) -> bool:
        scope = self._scope(owner_id, project_id, agent_id)
        with self._lock, self._connect() as db:
            row = db.execute("""SELECT checkpoint,predecessor,rotation
                FROM web10_operations WHERE owner=? AND project=? AND agent=?
                AND task=?""", (*scope, task_id)).fetchone()
        return row == (checkpoint_id, predecessor_id, rotation_id)

    def commit(self, receipt: WebRotationReceipt) -> None:
        scope = self._scope(receipt.owner_id, receipt.project_id, receipt.agent_id)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            operation = db.execute("""SELECT checkpoint,predecessor,rotation,
                target,status FROM web10_operations
                WHERE owner=? AND project=? AND agent=? AND task=?""",
                (*scope, receipt.task_id)).fetchone()
            if (operation is None or operation[:3] !=
                    (receipt.checkpoint_id, receipt.predecessor_id,
                     receipt.rotation_id)):
                raise WebChatRotationError("rotation journal operation mismatch")
            if operation[4] == "COMMITTED":
                if operation[3] != receipt.conversation_id:
                    raise WebChatRotationError("rotation journal target mismatch")
                return
            latest = db.execute("""SELECT ordinal, gpt_project, conversation,
                successor FROM web10_chats
                WHERE owner=? AND project=? AND agent=?
                ORDER BY ordinal DESC LIMIT 1""", scope).fetchone()
            if (latest is None or latest[2] != receipt.predecessor_id
                    or latest[1] != receipt.chatgpt_project_id
                    or latest[3] is not None):
                raise WebChatRotationError("journal lineage changed")
            try:
                db.execute("""INSERT INTO web10_chats VALUES
                    (?,?,?,?,?,?,?,?,?,?)""", (*scope, latest[0] + 1,
                    receipt.chatgpt_project_id, receipt.conversation_id,
                    receipt.predecessor_id, None, receipt.task_id,
                    receipt.rotation_id))
            except sqlite3.IntegrityError:
                raise WebChatRotationError("duplicate committed rotation") from None
            db.execute("""UPDATE web10_chats SET successor=?
                WHERE owner=? AND project=? AND agent=? AND ordinal=?""",
                (receipt.conversation_id, *scope, latest[0]))
            db.execute("""UPDATE web10_operations SET target=?, status='COMMITTED'
                WHERE owner=? AND project=? AND agent=? AND task=?""",
                (receipt.conversation_id, *scope, receipt.task_id))


class WebAutomaticChatRotator:
    """Consume a verified WEB-09 rotation signal without rerunning the task.

    Keeps the task/checkpoint unchanged, requires an idempotent mock ChatPort,
    and never sends a model turn. Real UI and TaskInbox integration are HOLD.
    """

    def __init__(self, sessions: WebAgentSessionRegistry,
                 chats: ProjectChatManager, journal: SQLiteWebRotationJournal,
                 monitor: WebContextMonitor):
        if (not isinstance(sessions, WebAgentSessionRegistry)
                or not isinstance(chats, ProjectChatManager)
                or chats._sessions is not sessions
                or not isinstance(journal, SQLiteWebRotationJournal)
                or not isinstance(monitor, WebContextMonitor)
                or monitor.sessions is not sessions
                or monitor._chats is not chats):
            raise ValueError("trusted rotation components required")
        self._sessions = sessions
        self._chats = chats
        self._journal = journal
        self._monitor = monitor

    def track_initial(self, *, owner_id: str, project_id: str,
                      agent_id: str) -> None:
        records = self._chats.history(owner_id=owner_id, project_id=project_id,
                                      agent_id=agent_id)
        if len(records) != 1:
            raise WebChatRotationError("expected one initial project chat")
        self._journal.seed_initial(records[0])

    def restore_agent(self, *, owner_id: str, project_id: str,
                      agent_id: str) -> tuple[ProjectChatRecord, ...]:
        """Reconstruct mock lineage from durable journal on a fresh manager."""
        records = self._journal.history(owner_id=owner_id, project_id=project_id,
                                         agent_id=agent_id)
        if not records:
            raise WebChatRotationError("no persisted mock chat lineage")
        self._chats.restore_verified(owner_id=owner_id, project_id=project_id,
                                     agent_id=agent_id, records=records)
        return records

    def rotate_on_pressure(self, *, owner_id: str, project_id: str,
                           agent_id: str, task_id: str,
                           checkpoint_id: str) -> WebRotationReceipt:
        """Automatic offline action from the last verified WEB-09 signal.

        A caller can invoke this after a context-pressure refusal without
        manually constructing a rotation decision or re-sending the task.
        """
        decision = self._monitor.last_decision(
            owner_id=owner_id, project_id=project_id, agent_id=agent_id)
        if decision is None:
            raise WebChatRotationError("no verified context rotation pressure")
        return self.rotate(decision=decision, task_id=task_id,
                           checkpoint_id=checkpoint_id)

    def rotate(self, *, decision: ContextPressureDecision,
               task_id: str, checkpoint_id: str) -> WebRotationReceipt:
        if (type(decision) is not ContextPressureDecision
                or decision.action not in ("ROTATE", "EMERGENCY")
                or decision.projected_upper_tokens is None
                or any(type(x) is not str or not _ID.fullmatch(x)
                       for x in (task_id, checkpoint_id))):
            raise WebChatRotationError("unverified rotation trigger")
        scope = dict(owner_id=decision.owner_id, project_id=decision.project_id,
                     agent_id=decision.agent_id)
        binding = self._sessions.project_binding(**scope)
        operation_id = _rotation_id(*scope.values(), task_id,
                                    checkpoint_id, decision.conversation_id)
        # A new operation must originate from this process's verified WEB-09
        # result. After a crash, the durable PREPARED record permits recovery
        # even though the transient decision object no longer exists.
        if (self._monitor.last_decision(**scope) is not decision
                and not self._journal.has_operation(
                    **scope, task_id=task_id, checkpoint_id=checkpoint_id,
                    predecessor_id=decision.conversation_id,
                    rotation_id=operation_id)):
            raise WebChatRotationError("rotation pressure proof unavailable")
        try:
            status, target = self._journal.prepare(
                **scope, task_id=task_id, checkpoint_id=checkpoint_id,
                predecessor_id=decision.conversation_id,
                rotation_id=operation_id)
        except WebChatRotationError:
            raise
        except Exception:
            raise WebChatRotationError("rotation journal unavailable") from None
        if status == "COMMITTED":
            records = self._journal.history(**scope)
            record = next((r for r in records
                           if r.rotation_id == operation_id), None)
            if record is None or record.conversation_id != target:
                raise WebChatRotationError("committed rotation proof missing")
        else:
            record = self._chats.rotate_idempotent(
                **scope, expected_predecessor=decision.conversation_id,
                task_id=task_id, rotation_id=operation_id)
        if (record.chatgpt_project_id != binding.chatgpt_project_id
                or record.predecessor_id != decision.conversation_id
                or record.task_id != task_id
                or record.rotation_id != operation_id):
            raise WebChatRotationError("rotation scope or task mismatch")
        receipt = WebRotationReceipt(*scope.values(), binding.chatgpt_project_id,
                                    task_id, checkpoint_id,
                                    decision.conversation_id,
                                    record.conversation_id, operation_id)
        if status != "COMMITTED":
            try:
                self._journal.commit(receipt)
            except WebChatRotationError:
                raise
            except Exception:
                raise WebChatRotationError("rotation journal commit unverified") from None
        return receipt
