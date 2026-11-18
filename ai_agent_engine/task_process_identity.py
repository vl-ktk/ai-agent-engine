"""Stage 6.9: Linux boot+PID-start identity, checked before any group signal.

PID numbers alone are never authority. A pidfd pins the leader PID while
the caller validates its original /proc start ticks and private session.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re


_BOOT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class ProcessIdentityUnavailable(RuntimeError):
    """Process identity cannot be verified without ambiguity."""


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    pgid: int
    sid: int
    start_ticks: int
    boot_id: str


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text("ascii").strip()
    except OSError:
        raise ProcessIdentityUnavailable("cannot identify host boot") from None
    if not _BOOT.fullmatch(value):
        raise ProcessIdentityUnavailable("invalid host boot identity")
    return value


def _proc_details(pid: int) -> tuple[str, int, int, int] | None:
    if type(pid) is not int or pid <= 1:
        raise ProcessIdentityUnavailable("unsafe process identifier")
    try:
        raw = Path(f"/proc/{pid}/stat").read_text("ascii")
    except FileNotFoundError:
        return None
    except OSError:
        raise ProcessIdentityUnavailable("cannot inspect process identity") from None
    try:
        fields = raw.rsplit(")", 1)[1].split()
        # The remaining fields start at the Linux stat(5) state (field 3).
        state, pgid, sid, start = fields[0], int(fields[2]), int(fields[3]), int(fields[19])
    except (IndexError, ValueError):
        raise ProcessIdentityUnavailable("unreadable process identity") from None
    if start <= 0:
        raise ProcessIdentityUnavailable("invalid process start time")
    return state, pgid, sid, start


def capture_private_leader(pid: int) -> ProcessIdentity:
    info = _proc_details(pid)
    if info is None:
        raise ProcessIdentityUnavailable("managed process already exited")
    state, pgid, sid, start = info
    if state in ("Z", "X", "x") or pgid != pid or sid != pid or pid == os.getpgrp():
        raise ProcessIdentityUnavailable("process is not a live private leader")
    return ProcessIdentity(pid, pgid, sid, start, _boot_id())


def matches_leader(identity: ProcessIdentity) -> bool:
    if _boot_id() != identity.boot_id:
        return False
    current = _proc_details(identity.pid)
    return (current is not None
            and current[1:] == (identity.pgid, identity.sid, identity.start_ticks))


def verified_leader_pidfd(identity: ProcessIdentity) -> int:
    """Pin the original process before a checked killpg, or refuse to signal."""
    if not hasattr(os, "pidfd_open") or identity.pid == os.getpid():
        raise ProcessIdentityUnavailable("pidfd-based recovery unavailable")
    try:
        fd = os.pidfd_open(identity.pid, 0)
    except (OSError, ProcessLookupError):
        raise ProcessIdentityUnavailable("original leader is not available") from None
    try:
        if not matches_leader(identity):
            raise ProcessIdentityUnavailable("process PID identity changed")
        return fd
    except BaseException:
        os.close(fd)
        raise


def group_has_live_members(pgid: int) -> bool:
    """Zombies are inert; any inaccessible /proc entry fails closed."""
    if type(pgid) is not int or pgid <= 1 or pgid == os.getpgrp():
        raise ProcessIdentityUnavailable("unsafe process group")
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdecimal() or int(entry.name) <= 1:
                    continue
                details = _proc_details(int(entry.name))
                if details is not None and details[1] == pgid and details[0] not in (
                        "Z", "X", "x"):
                    return True
        return False
    except OSError:
        raise ProcessIdentityUnavailable("cannot verify process group") from None
