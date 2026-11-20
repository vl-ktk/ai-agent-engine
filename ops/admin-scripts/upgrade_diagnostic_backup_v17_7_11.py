"""Safely install the independently reviewed v17-compatible backup hook.

Requires the exact historical v14 hook, live release, owner request and a
passing scratch restore. Replaces only one fixed executable atomically and
preserves the original in private root evidence for recovery. No database
or timer writes, no backup creation, no permission or credential expansion.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess


LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
OLD_HOOK_SHA256 = "9930b6d4d34d751cc661fe7e20c2527508715056e35139d7a59ea2db554cdb5a"
NEW_HOOK_SHA256 = "62ef63e2c9fca437ce2ab0944a6f677258001011ea1045422a460d0c59d9ddc0"
CURRENT = Path("/opt/ai-agent-engine/current")
HOOK = Path("/usr/local/libexec/ae-diagnostic-backup.py")
SOURCE = Path("ai_agent_engine/diagnostic_backup_v17.py")
TASKS = Path("/var/lib/ai-agent-engine-diagnostics/tasks.sqlite3")
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


_CHECK = """
import importlib.util, json
spec = importlib.util.spec_from_file_location('installed_diagnostic_backup',
    '/usr/local/libexec/ae-diagnostic-backup.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
backup = mod.DiagnosticBackup()
shots = backup._snapshots()
if not shots:
    raise RuntimeError('no snapshots')
backup.verify(shots[-1])
restored = backup.restore_smoke(shots[-1])
if restored['status'] != 'RESTORE_SMOKE_PASS':
    raise RuntimeError('restore failed')
print(json.dumps({'snapshot_count': len(shots), 'verified': True, 'restored': True}))
"""


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_file(path: Path, owner: int, mode: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == owner
            and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1)


def _read_installed() -> bytes:
    if not _private_file(HOOK, 0, 0o755):
        raise RuntimeError("installed backup hook metadata changed")
    with os.fdopen(os.open(HOOK, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as f:
        data = f.read(128 * 1024 + 1)
    if len(data) > 128 * 1024:
        raise RuntimeError("installed backup hook too large")
    return data


def _atomic_install(data: bytes, request_id: str) -> None:
    if not 0 < len(data) < 128 * 1024:
        raise RuntimeError("invalid reviewed installer payload")
    temp = HOOK.parent / (".ae-diagnostic-backup-" + request_id + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o755)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o755)
            os.fsync(output.fileno())
        os.replace(temp, HOOK)
    finally:
        if temp.exists() and not temp.is_symlink():
            temp.unlink()


def _restore_check(runtime) -> int:
    def drop():
        os.setgroups([])
        os.setgid(runtime.pw_gid)
        os.setuid(runtime.pw_uid)

    checked = subprocess.run(
        ("/usr/bin/python3", "-I", "-c", _CHECK),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=150, check=False, preexec_fn=drop,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    if checked.returncode:
        raise RuntimeError("backup verification or disposable restore failed")
    receipt = json.loads(checked.stdout)
    if (receipt.get("verified") is not True or receipt.get("restored") is not True
            or type(receipt.get("snapshot_count")) is not int
            or not 1 <= receipt["snapshot_count"] <= 100):
        raise RuntimeError("invalid private backup verification receipt")
    return receipt["snapshot_count"]


def main() -> None:
    if os.geteuid() != 0 or not all(os.environ.get(key) for key in
                                   ("AE_REQUEST_SHA", "AE_SOURCE_SHA", "AE_EVIDENCE", "AE_REQUEST_ID")):
        raise RuntimeError("owner-authenticated immutable admin request required")
    expected = os.environ.get("AE_EXPECTED_CURRENT")
    if expected != LIVE_SHA or os.readlink(CURRENT) != "releases/" + expected:
        raise RuntimeError("Netcup live release changed; HOLD")
    for unit in UNITS:
        if subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", unit),
                          stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=8, check=False).returncode:
            raise RuntimeError("baseline unit stopped; HOLD")
    effective = subprocess.run(
        ("/usr/bin/systemctl", "show", "-p", "ExecStartPost", "ae-backup.service"),
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=8, check=True,
    ).stdout
    if str(HOOK) not in effective:
        raise RuntimeError("backup service is not wired to reviewed diagnostic hook; HOLD")
    original = _read_installed()
    if _hash(original) != OLD_HOOK_SHA256:
        raise RuntimeError("historically reviewed original changed; HOLD")
    candidate = SOURCE.read_bytes()
    if SOURCE.is_symlink() or _hash(candidate) != NEW_HOOK_SHA256:
        raise RuntimeError("reviewed new backup hook digest mismatch")
    runtime = pwd.getpwnam("ae-runtime")
    if not _private_file(TASKS, runtime.pw_uid, 0o600):
        raise RuntimeError("task SQLite missing or unsafe")
    if any(Path(str(TASKS) + end).exists() for end in ("-wal", "-shm", "-journal")):
        raise RuntimeError("task SQLite has active sidecar; HOLD")
    with sqlite3.connect("file:" + str(TASKS) + "?mode=ro&immutable=1", uri=True) as db:
        app = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        integrity = db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    if app != 0x4145494E or version != 14 or not integrity:
        raise RuntimeError("live task SQLite baseline changed; HOLD")
    before_count = _restore_check(runtime)

    evidence = Path(os.environ["AE_EVIDENCE"])
    info = evidence.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("unsafe private evidence directory")
    rollback = evidence / "original-diagnostic-backup.py"
    with rollback.open("xb") as saved:
        saved.write(original)
        saved.flush()
        os.fchmod(saved.fileno(), 0o600)
        os.fsync(saved.fileno())
    request_id = os.environ["AE_REQUEST_ID"]
    changed = False
    try:
        if (os.readlink(CURRENT) != "releases/" + expected
                or _hash(_read_installed()) != OLD_HOOK_SHA256):
            raise RuntimeError("live release or installed hook changed before replacement; HOLD")
        _atomic_install(candidate, request_id)
        changed = True
        if _hash(_read_installed()) != NEW_HOOK_SHA256:
            raise RuntimeError("new installed backup digest mismatch")
        after_count = _restore_check(runtime)
        if before_count != after_count:
            raise RuntimeError("snapshot inventory changed during installation")
        report = {"status": "V17_COMPATIBLE_BACKUP_HOOK_INSTALLED",
                  "scope": "vl-ktk/ai-agent-engine", "live_release": expected,
                  "task_sqlite_schema_unchanged": 14, "backup_count": after_count,
                  "existing_snapshot_verify": "PASS", "scratch_restore": "PASS",
                  "original_preserved_in_private_evidence": True,
                  "controller_updated": False, "working_db_modified": False,
                  "backup_created": False, "services_restarted": False}
        with (evidence / "diagnostic-backup-v17-install.json").open("x", encoding="utf-8") as file:
            json.dump(report, file, sort_keys=True, separators=(",", ":"))
            file.write("\n")
    except BaseException:
        if changed:
            _atomic_install(original, request_id)
        raise
    print("DIAGNOSTIC_BACKUP_V17_7_11 " + json.dumps(report, sort_keys=True,
                                                     separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
