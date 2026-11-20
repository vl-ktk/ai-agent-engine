"""Read-only attestation of the installed, historically reviewed Netcup backup.

Only exact known SHA-256 versions may be loaded, unprivileged, for a verify
and scratch-only restore. Never create/rotate backups or mutate task SQLite.
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
CURRENT = Path("/opt/ai-agent-engine/current")
HOOK = Path("/usr/local/libexec/ae-diagnostic-backup.py")
TASKS = Path("/var/lib/ai-agent-engine-diagnostics/tasks.sqlite3")
BACKUP_ROOT = Path("/var/backups/ai-agent-engine-diagnostics")
KNOWN = {
    "9930b6d4d34d751cc661fe7e20c2527508715056e35139d7a59ea2db554cdb5a": ("historical_v14", (14,)),
    "6ef4e3d0de703a20b6ffaee1968ad78f8dcef022283809e32466d72bdc94304d": ("reviewed_v14_v15", (14, 15)),
}
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")

_VERIFY = """
import importlib.util, json
spec = importlib.util.spec_from_file_location('known_diagnostic_backup',
    '/usr/local/libexec/ae-diagnostic-backup.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
backup = mod.DiagnosticBackup()
shots = backup._snapshots()
if not shots:
    raise RuntimeError('no diagnostic snapshots')
backup.verify(shots[-1])
out = backup.restore_smoke(shots[-1])
if out['status'] != 'RESTORE_SMOKE_PASS':
    raise RuntimeError('restore verification failed')
print(json.dumps({'count': len(shots), 'verified': True, 'restored': True}))
"""


def _private_regular(path: Path, *, uid: int, mode: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == uid
            and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1)


def _known_version(path: Path) -> tuple[str, tuple[int, ...]]:
    if not _private_regular(path, uid=0, mode=0o755):
        raise RuntimeError("installed backup hook has unsafe metadata")
    # A known source hash is a prerequisite for loading any host executable.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW), "rb") as f:
        data = f.read(128 * 1024 + 1)
    if len(data) > 128 * 1024:
        raise RuntimeError("installed backup hook exceeds reviewed size")
    known = KNOWN.get(hashlib.sha256(data).hexdigest())
    if known is None:
        raise RuntimeError("installed backup hook is not historically reviewed")
    return known


def _task_schema(uid: int) -> int:
    if not _private_regular(TASKS, uid=uid, mode=0o600):
        raise RuntimeError("task SQLite missing or unsafe")
    if any(Path(str(TASKS) + end).exists() for end in ("-wal", "-shm", "-journal")):
        raise RuntimeError("task SQLite has pending sidecar")
    with sqlite3.connect("file:" + str(TASKS) + "?mode=ro&immutable=1", uri=True) as db:
        app = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        valid = db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    if app != 0x4145494E or not valid:
        raise RuntimeError("task SQLite identity/integrity failed")
    return version


def _restore_check(uid: int, gid: int) -> dict:
    def drop():
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    result = subprocess.run(
        ("/usr/bin/python3", "-I", "-c", _VERIFY),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=150, check=False, preexec_fn=drop,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    if result.returncode:
        raise RuntimeError("existing backup verify/restore smoke failed")
    record = json.loads(result.stdout)
    if (record.get("verified") is not True or record.get("restored") is not True
            or type(record.get("count")) is not int or not 1 <= record["count"] <= 100):
        raise RuntimeError("invalid backup verification receipt")
    return {"snapshot_count": record["count"], "verify": "PASS", "restore_smoke": "PASS"}


def main() -> None:
    if os.geteuid() != 0 or not all(os.environ.get(name) for name in
                                   ("AE_SOURCE_SHA", "AE_REQUEST_SHA", "AE_EVIDENCE")):
        raise RuntimeError("authenticated owner-admin audit required")
    expected = os.environ.get("AE_EXPECTED_CURRENT")
    if expected != LIVE_SHA or os.readlink(CURRENT) != "releases/" + expected:
        raise RuntimeError("live release changed; HOLD")
    for unit in UNITS:
        if subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", unit),
                          stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=8, check=False).returncode:
            raise RuntimeError("baseline unit inactive; HOLD")
    name, accepted = _known_version(HOOK)
    runtime = pwd.getpwnam("ae-runtime")
    version = _task_schema(runtime.pw_uid)
    report = {
        "schema": 1, "scope": "vl-ktk/ai-agent-engine", "live_release": expected,
        "installed_hook_reviewed_version": name,
        "installed_hook_supports_task_schema": version in accepted,
        "actual_task_schema": version,
        "latest_backup": _restore_check(runtime.pw_uid, runtime.pw_gid),
        "working_sqlite_modified": False, "backup_created": False,
        "hook_replaced": False, "service_changed": False,
        "status": "READ_ONLY_ATTESTED_HOLD_V17_MIGRATION",
    }
    evidence = Path(os.environ["AE_EVIDENCE"])
    info = evidence.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("unsafe owner evidence directory")
    with (evidence / "historical-backup-audit-7-11.json").open("x", encoding="utf-8") as file:
        json.dump(report, file, sort_keys=True, separators=(",", ":"))
        file.write("\n")
    print("HISTORICAL_BACKUP_AUDIT_7_11 " + json.dumps(report, sort_keys=True,
                                                        separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
