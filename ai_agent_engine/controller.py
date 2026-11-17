"""Minimal always-on lifecycle controller for the single-owner host.

This is deliberately *not* the task orchestrator (roadmap stage 5). It owns a
private heartbeat, a single-instance lock, and systemd readiness/watchdog
signals. It does not call any model, read credentials, open ports or run tools.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import sys
from tempfile import NamedTemporaryFile
from threading import Event
import time
from typing import Iterator


DEFAULT_HEALTH_FILE = Path("/run/ai-agent-engine/controller-health.json")
DEFAULT_INTERVAL = 5.0
MAX_HEARTBEAT_AGE = 15.0


def _notify(message: str) -> None:
    """Send one systemd notification, failing if a configured socket is broken."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("systemd notifications require an AF_UNIX socket")
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        try:
            client.sendto(message.encode("utf-8"), address)
        except OSError as exc:
            raise RuntimeError("systemd notification socket unavailable") from exc


def _write_heartbeat(file: Path, *, started: float) -> None:
    if not file.is_absolute() or not file.parent.is_dir():
        raise RuntimeError("heartbeat needs an existing absolute private directory")
    timestamp = time.time()
    record = {
        "schema": 1,
        "service": "ai-agent-engine-controller",
        "status": "ready",
        "pid": os.getpid(),
        "started_utc": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "heartbeat_unix": timestamp,
        "uptime_seconds": max(0, timestamp - started),
    }
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=file.parent,
            prefix=".controller-heartbeat-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    # The service is explicitly Linux/POSIX-only. os.kill(pid, 0) must never
    # be used as a Windows process probe (its semantics are platform-specific).
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def check_health(file: Path, *, max_age: float = MAX_HEARTBEAT_AGE) -> bool:
    """Fail closed on missing/stale/malformed heartbeat or absent process."""
    if max_age <= 0:
        raise ValueError("max_age must be positive")
    try:
        if file.is_symlink() or file.stat().st_size > 4096:
            return False
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        heartbeat = data.get("heartbeat_unix")
        pid = data.get("pid")
        if (data.get("schema") != 1 or data.get("service") != "ai-agent-engine-controller"
                or data.get("status") != "ready" or type(heartbeat) not in (int, float)
                or type(pid) is not int or pid <= 0):
            return False
        age = time.time() - heartbeat
        return 0 <= age <= max_age and _alive(pid)
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


@contextmanager
def _single_instance(file: Path) -> Iterator[None]:
    """Lock in the service runtime directory; kernel releases it on crash."""
    if os.name != "posix":
        raise RuntimeError("controller service requires a POSIX host")
    import fcntl  # Unix-only: keep module importable for Windows CI.

    if not file.is_absolute() or not file.parent.is_dir():
        raise RuntimeError("controller needs an existing absolute runtime directory")
    lock = file.with_suffix(".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("controller is already running") from exc
        yield
    finally:
        os.close(descriptor)


def run(file: Path, *, interval: float = DEFAULT_INTERVAL) -> None:
    if not 0.05 <= interval <= 10.0:
        raise ValueError("heartbeat interval must be between 0.05 and 10 seconds")
    stopped = Event()

    def on_signal(_number: int, _frame: object) -> None:
        stopped.set()

    with _single_instance(file):
        previous_term = signal.signal(signal.SIGTERM, on_signal)
        previous_int = signal.signal(signal.SIGINT, on_signal)
        started = time.time()
        try:
            _write_heartbeat(file, started=started)
            _notify("READY=1\nSTATUS=Controller lifecycle ready")
            while not stopped.wait(interval):
                _write_heartbeat(file, started=started)
                _notify("WATCHDOG=1")
        finally:
            try:
                _notify("STOPPING=1")
            finally:
                try:
                    # The single-instance lock prevents removal of another process's file.
                    file.unlink(missing_ok=True)
                finally:
                    signal.signal(signal.SIGTERM, previous_term)
                    signal.signal(signal.SIGINT, previous_int)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local systemd lifecycle controller; no model calls")
    actions = parser.add_subparsers(dest="action", required=True)
    start = actions.add_parser("run", help="run the persistent single-owner lifecycle process")
    start.add_argument("--health-file", type=Path, default=DEFAULT_HEALTH_FILE)
    start.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    check = actions.add_parser("check", help="verify the private controller heartbeat")
    check.add_argument("--health-file", type=Path, default=DEFAULT_HEALTH_FILE)
    check.add_argument("--max-age", type=float, default=MAX_HEARTBEAT_AGE)
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            healthy = check_health(args.health_file, max_age=args.max_age)
            print("controller healthy" if healthy else "controller unavailable")
            return 0 if healthy else 1
        run(args.health_file, interval=args.interval)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"controller stopped: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
