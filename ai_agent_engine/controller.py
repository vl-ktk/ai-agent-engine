"""Single-owner, unprivileged, persistent controller process.

Operates a bounded SQLite job queue and a real heartbeat/singleton lease.
This stage intentionally has just one offline core self-test handler: external
model routing, paid calls, general shell and network actions are NOT enabled.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
from uuid import uuid4

from .core import Action, Agent, ModelTurn, ScriptedModel
from .tools import SandboxFiles

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3
MAX_PENDING = 1000
KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
ALLOWED_KINDS = ("core_self_test",)


class ControllerError(RuntimeError):
    """A fail-closed, non-secret controller error."""


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ControllerError("State directory cannot be a symlink")
    try:
        info = path.stat()
    except OSError:
        raise ControllerError("State directory must already exist") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ControllerError("State path is not a directory")
    if os.name == "posix":
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ControllerError("State directory must be private and owned by the runtime user")
    return path.resolve(strict=True)


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class ControllerStore:
    """SQLite storage isolated to one already-private service state directory."""

    def __init__(self, state_dir: Path):
        self.directory = private_directory(state_dir)
        self.database = self.directory / "controller.sqlite3"

    def connect(self) -> sqlite3.Connection:
        if self.database.is_symlink():
            raise ControllerError("Database path cannot be a symlink")
        if self.database.exists() and not self.database.is_file():
            raise ControllerError("Database path must be a regular file")
        old_umask = os.umask(0o077)
        try:
            conn = sqlite3.connect(str(self.database), timeout=5, isolation_level=None)
        finally:
            os.umask(old_umask)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS singleton (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    owner TEXT,
                    lease_until REAL NOT NULL DEFAULT 0,
                    heartbeat REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO singleton (id) VALUES (1);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind = 'core_self_test'),
                    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    result_code TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created);
            """)
            if os.name == "posix":
                mode = self.database.stat().st_mode
                if mode & 0o077:
                    raise ControllerError("Database permissions are not private")
            return conn
        except BaseException:
            conn.close()
            raise

    def inspect(self) -> dict[str, object]:
        if self.database.is_symlink() or not self.database.is_file():
            raise ControllerError("Controller state has not been initialized")
        # Read-only status/health does not create or modify the database.
        conn = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True, timeout=2)
        try:
            state = conn.execute("SELECT owner, lease_until, heartbeat FROM singleton WHERE id=1").fetchone()
            counts = {status: count for status, count in conn.execute(
                "SELECT state, COUNT(*) FROM jobs GROUP BY state"
            )}
        finally:
            conn.close()
        if state is None:
            raise ControllerError("Missing singleton controller record")
        now = time.time()
        age = max(0.0, now - state[2]) if state[2] > 0 else None
        active = bool(state[0]) and state[1] > now and age is not None and age <= LEASE_SECONDS
        return {
            "active": active,
            "heartbeat_age_seconds": round(age, 2) if age is not None else None,
            "jobs": {status: counts.get(status, 0) for status in ("pending", "running", "done", "failed")},
        }

    def enqueue(self, conn: sqlite3.Connection, kind: str, key: str | None) -> str:
        if kind not in ALLOWED_KINDS:
            raise ControllerError("Unsupported job kind")
        if key is not None and not KEY_PATTERN.fullmatch(key):
            raise ControllerError("Invalid idempotency key")
        job_id = key if key is not None else uuid4().hex
        now = time.time()
        with transaction(conn):
            present = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if present is not None:
                return present["id"]
            queued = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('pending', 'running')"
            ).fetchone()[0]
            if queued >= MAX_PENDING:
                raise ControllerError("Queue capacity reached")
            conn.execute(
                "INSERT INTO jobs(id,kind,state,attempts,created,updated) "
                "VALUES(?,?,'pending',0,?,?)", (job_id, kind, now, now)
            )
        return job_id

    def acquire(self, conn: sqlite3.Connection, owner: str) -> None:
        now = time.time()
        with transaction(conn):
            row = conn.execute("SELECT owner,lease_until FROM singleton WHERE id=1").fetchone()
            if row["owner"] and row["lease_until"] > now and row["owner"] != owner:
                raise ControllerError("Another live controller holds the state lease")
            conn.execute(
                "UPDATE singleton SET owner=?,lease_until=?,heartbeat=? WHERE id=1",
                (owner, now + LEASE_SECONDS, now)
            )
            # Any prior process died or lost the lease. Only self-test jobs are retried.
            conn.execute(
                "UPDATE jobs SET state='failed',result_code='retry_limit',updated=? "
                "WHERE state='running' AND attempts>=?", (now, MAX_ATTEMPTS)
            )
            conn.execute(
                "UPDATE jobs SET state='pending',updated=? "
                "WHERE state='running' AND attempts<?", (now, MAX_ATTEMPTS)
            )

    def heartbeat(self, conn: sqlite3.Connection, owner: str) -> None:
        now = time.time()
        with transaction(conn):
            updated = conn.execute(
                "UPDATE singleton SET heartbeat=?,lease_until=? "
                "WHERE id=1 AND owner=? AND lease_until>?",
                (now, now + LEASE_SECONDS, owner, now)
            )
            if updated.rowcount != 1:
                raise ControllerError("Controller lease was lost")

    def claim(self, conn: sqlite3.Connection) -> tuple[str, str] | None:
        now = time.time()
        with transaction(conn):
            row = conn.execute(
                "SELECT id,kind FROM jobs WHERE state='pending' "
                "ORDER BY created,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,updated=? "
                "WHERE id=? AND state='pending'", (now, row["id"])
            )
            return (row["id"], row["kind"])

    def finish(self, conn: sqlite3.Connection, job_id: str, succeeded: bool) -> None:
        now = time.time()
        with transaction(conn):
            conn.execute(
                "UPDATE jobs SET state=?,result_code=?,updated=? "
                "WHERE id=? AND state='running'",
                ("done" if succeeded else "failed",
                 "core_self_test_pass" if succeeded else "core_self_test_failure",
                 now, job_id)
            )

    def release(self, conn: sqlite3.Connection, owner: str) -> None:
        with transaction(conn):
            conn.execute(
                "UPDATE singleton SET owner=NULL,lease_until=0 WHERE id=1 AND owner=?",
                (owner,)
            )


def run_core_self_test() -> bool:
    """Execute the real bounded Agent/tool engine, but with a synthetic local model."""
    with TemporaryDirectory(prefix="ae-controller-probe-") as directory:
        sandbox = SandboxFiles(Path(directory))
        turns = (
            ModelTurn(action=Action("write_new_file", {"path": "probe.txt", "content": "ok"})),
            ModelTurn(action=Action("read_file", {"path": "probe.txt"})),
            ModelTurn(final_text="Core probe passed"),
        )
        result = Agent(model=ScriptedModel(turns), tools=sandbox.allowed_tools()).run(
            "Check isolated core execution"
        )
        return result.status == "done" and result.steps == 3


def serve(store: ControllerStore, *, poll: float, once: bool = False) -> None:
    if os.name == "posix" and os.geteuid() == 0:
        raise ControllerError("Controller must never run as root")
    if not 0.05 <= poll <= 60:
        raise ControllerError("Poll interval out of range")
    stop = Event()
    if not once:
        def request_stop(_signum, _frame):
            stop.set()
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
    owner = uuid4().hex
    conn = store.connect()
    acquired = False
    try:
        store.acquire(conn, owner)
        acquired = True
        print("controller_started", flush=True)
        while not stop.is_set():
            store.heartbeat(conn, owner)
            claimed = store.claim(conn)
            if claimed is not None:
                job_id, kind = claimed
                try:
                    success = kind == "core_self_test" and run_core_self_test()
                except Exception:
                    success = False
                store.finish(conn, job_id, success)
                print("job_completed" if success else "job_failed", flush=True)
            if once:
                break
            stop.wait(poll)
    finally:
        if acquired:
            store.release(conn, owner)
        conn.close()
        print("controller_stopped", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "run-once", "enqueue", "status", "health"):
        action = actions.add_parser(name)
        action.add_argument("--state-dir", type=Path, required=True)
        if name == "serve":
            action.add_argument("--poll-interval", type=float, default=1)
        if name == "enqueue":
            action.add_argument("--kind", choices=ALLOWED_KINDS, default="core_self_test")
            action.add_argument("--idempotency-key")
        if name == "health":
            action.add_argument("--max-age", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        store = ControllerStore(args.state_dir)
        if args.command in ("serve", "run-once"):
            serve(store, poll=args.poll_interval if args.command == "serve" else 0.1,
                  once=args.command == "run-once")
        elif args.command == "enqueue":
            with store.connect() as conn:
                job_id = store.enqueue(conn, args.kind, args.idempotency_key)
            print(json.dumps({"job_id": job_id, "queued": True}))
        else:
            state = store.inspect()
            if args.command == "health":
                if not 0.05 <= args.max_age <= 60:
                    raise ControllerError("Health age out of range")
                healthy = bool(state["active"]) and state["heartbeat_age_seconds"] <= args.max_age
                print(json.dumps({"healthy": healthy}))
                return 0 if healthy else 1
            print(json.dumps(state, sort_keys=True))
        return 0
    except (ControllerError, OSError, sqlite3.Error, ValueError):
        # Do not include raw exception text from SQLite/filesystem in service logs.
        print("controller_error: validation, state or lease failure", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
