"""Owner-gated, read-only Netcup audit before ANY task SQLite v17 rollout.

Does not deploy, migrate, write working data, start services, create backups,
read secrets or print task records. The restore check uses disposable local
storage and the already-installed, SHA-pinned diagnostic backup executable.
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
TASKS = Path("/var/lib/ai-agent-engine-diagnostics/tasks.sqlite3")
BACKUP = Path("/usr/local/libexec/ae-diagnostic-backup.py")
BACKUP_SHA256 = "6ef4e3d0de703a20b6ffaee1968ad78f8dcef022283809e32466d72bdc94304d"
BACKUP_ROOT = Path("/var/backups/ai-agent-engine-diagnostics")
DROPIN = Path("/etc/systemd/system/ae-backup.service.d/70-diagnostic-project.conf")
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")

# The subprocess is unprivileged. Loading this exact pinned executable only
# calls verification and a scratch restore, never `create` or retention cleanup.
_VERIFY = """
import importlib.util
import json
from pathlib import Path
spec = importlib.util.spec_from_file_location('installed_diagnostic_backup',
    '/usr/local/libexec/ae-diagnostic-backup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
backup = module.DiagnosticBackup()
snapshots = backup._snapshots()
if not snapshots:
    raise RuntimeError('no diagnostic snapshots')
backup.verify(snapshots[-1])
restore = backup.restore_smoke(snapshots[-1])
if restore['status'] != 'RESTORE_SMOKE_PASS':
    raise RuntimeError('restore smoke failed')
print(json.dumps({'verified': True, 'restore_smoke': True,
                  'snapshot_count': len(snapshots)}))
"""


def _private_regular(path: Path, uid: int, mode: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == uid
            and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema() -> dict:
    if not _private_regular(TASKS, pwd.getpwnam("ae-runtime").pw_uid, 0o600):
        raise RuntimeError("diagnostic SQLite missing or unsafe")
    if any(Path(str(TASKS) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise RuntimeError("diagnostic SQLite has sidecars; cannot make immutable assessment")
    with sqlite3.connect("file:" + str(TASKS) + "?mode=ro&immutable=1", uri=True) as db:
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        valid = db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    if app_id != 0x4145494E or not valid:
        raise RuntimeError("diagnostic SQLite identity or integrity failed")
    return {"application_id_valid": True, "schema_version": version, "quick_check": "PASS"}


def _restore_check(uid: int, gid: int) -> dict:
    def drop_privileges() -> None:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    completed = subprocess.run(
        ("/usr/bin/python3", "-I", "-c", _VERIFY),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=150, check=False, preexec_fn=drop_privileges,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    if completed.returncode:
        raise RuntimeError("diagnostic backup verify/restore smoke failed")
    record = json.loads(completed.stdout)
    if (type(record.get("snapshot_count")) is not int
            or not 1 <= record["snapshot_count"] <= 100
            or record.get("verified") is not True
            or record.get("restore_smoke") is not True):
        raise RuntimeError("invalid restore audit result")
    return record


def main() -> None:
    if os.geteuid() != 0 or not all(os.environ.get(name) for name in
                                   ("AE_REQUEST_SHA", "AE_SOURCE_SHA", "AE_EVIDENCE")):
        raise RuntimeError("authenticated owner-admin audit required")
    expected = os.environ.get("AE_EXPECTED_CURRENT")
    if expected != LIVE_SHA or os.readlink(CURRENT) != "releases/" + expected:
        raise RuntimeError("Netcup live release changed; HOLD")
    for unit in UNITS:
        if subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", unit),
                          stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=8, check=False).returncode:
            raise RuntimeError("baseline service inactive; HOLD")
    if not _private_regular(BACKUP, 0, 0o755) or _hash(BACKUP) != BACKUP_SHA256:
        raise RuntimeError("installed diagnostic backup differs from reviewed source; HOLD")
    expected_dropin = ("[Service]\n"
                       "ReadOnlyPaths=/var/lib/ai-agent-engine-diagnostics\n"
                       "ReadWritePaths=/var/backups/ai-agent-engine-diagnostics\n"
                       "ExecStartPost=/usr/bin/python3 /usr/local/libexec/ae-diagnostic-backup.py create\n")
    if not _private_regular(DROPIN, 0, 0o644) or DROPIN.read_text() != expected_dropin:
        raise RuntimeError("installed diagnostic backup drop-in differs; HOLD")
    uid = pwd.getpwnam("ae-runtime")
    report = {
        "status": "HOLD_V17_BACKUP_INCOMPATIBLE",
        "scope": "vl-ktk/ai-agent-engine",
        "live_release": expected,
        "baseline_units_active": True,
        "task_sqlite": _schema(),
        "installed_backup_sha256_matches_review": True,
        "installed_backup_supported_task_schemas": [14, 15],
        "installed_backup_supports_v17": False,
        "latest_diagnostic_backup": _restore_check(uid.pw_uid, uid.pw_gid),
        "live_data_modified": False,
        "new_backup_created": False,
        "deployment_performed": False,
    }
    evidence = Path(os.environ["AE_EVIDENCE"])
    metadata = evidence.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError("unsafe private evidence directory")
    (evidence / "backup-v17-audit-7-11.json").write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print("BACKUP_V17_AUDIT_7_11 " + json.dumps(report, sort_keys=True,
                                                separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
