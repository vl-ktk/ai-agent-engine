"""One-time owner-gated extension of existing daily backup service.

The existing backup executable and .service/.timer files are never replaced.
Only one new standalone diagnostic executable, one private destination, and
one reversible systemd drop-in are permitted. No diagnostic process is started.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys


CURRENT = Path("/opt/ai-agent-engine/current")
LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
BASE_UNIT = Path("/etc/systemd/system/ae-backup.service")
BASE_TIMER = Path("/etc/systemd/system/ae-backup.timer")
BASE_SCRIPT = Path("/usr/local/libexec/ae-backup.py")
BASE_HASHES = {
    BASE_UNIT: "80bfd9c338bf054a3e68908bedde2b3b5739c698724938a8ddaee82765b52c26",
    BASE_TIMER: "424f57f87cb82f7763e1c59183fea7137f9e16869948bba5f88c4fa5173c7ac3",
    BASE_SCRIPT: "0ba673121ba68a77d8b08307d85679db9ce768af280173b46611aa8528204c2b",
}
TARGET_SCRIPT = Path("/usr/local/libexec/ae-diagnostic-backup.py")
DROPIN_DIR = Path("/etc/systemd/system/ae-backup.service.d")
DROPIN = DROPIN_DIR / "70-diagnostic-project.conf"
BACKUP_ROOT = Path("/var/backups/ai-agent-engine-diagnostics")
SOURCE_ROOT = Path("/var/lib/ai-agent-engine-diagnostics")
REVIEWED_BACKUP_SHA256 = "9930b6d4d34d751cc661fe7e20c2527508715056e35139d7a59ea2db554cdb5a"
SERVICES = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def dropin_bytes() -> bytes:
    return (
        "[Service]\n"
        "ReadOnlyPaths=/var/lib/ai-agent-engine-diagnostics\n"
        "ReadWritePaths=/var/backups/ai-agent-engine-diagnostics\n"
        "ExecStartPost=/usr/bin/python3 /usr/local/libexec/ae-diagnostic-backup.py create\n"
    ).encode("ascii")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _private_root(path: Path, uid: int, *, mode: int) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeError("unexpected private host path")


def _baseline(expected: str) -> None:
    if expected != LIVE_SHA or os.readlink(CURRENT) != "releases/" + expected:
        raise RuntimeError("live server release changed")
    for path, digest in BASE_HASHES.items():
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != (0o755 if path == BASE_SCRIPT else 0o644)
                or _hash(path) != digest):
            raise RuntimeError("existing backup installation changed")
    for unit in SERVICES:
        check = subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", unit),
                               stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=8, check=False)
        if check.returncode:
            raise RuntimeError("baseline service stopped")


@contextmanager
def _as_runtime_user(uid: int, gid: int):
    old_uid, old_gid = os.geteuid(), os.getegid()
    os.setegid(gid)
    os.seteuid(uid)
    try:
        yield
    finally:
        os.seteuid(old_uid)
        os.setegid(old_gid)


def _new_file(path: Path, content: bytes, mode: int) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(fd, content[offset:])
        os.fsync(fd)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _command(*args: str) -> None:
    subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                   check=True, timeout=30)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("owner-admin root channel required")
    expected = os.environ.get("AE_EXPECTED_CURRENT")
    if (expected != LIVE_SHA or not os.environ.get("AE_REQUEST_SHA")
            or not os.environ.get("AE_SOURCE_SHA")):
        raise RuntimeError("missing exact immutable owner request")
    evidence = Path(os.environ["AE_EVIDENCE"])
    _private_root(evidence, 0, mode=0o700)
    _baseline(expected)
    uid = pwd.getpwnam("ae-runtime")
    source = Path.cwd() / "ai_agent_engine/diagnostic_backup.py"
    script_content = source.read_bytes()
    if (source.is_symlink() or hashlib.sha256(script_content).hexdigest()
            != REVIEWED_BACKUP_SHA256):
        raise RuntimeError("unexpected reviewed diagnostic backup code")
    sys.path.insert(0, str(Path.cwd()))
    from ai_agent_engine.diagnostic_backup import DiagnosticBackup
    with _as_runtime_user(uid.pw_uid, uid.pw_gid):
        DiagnosticBackup(SOURCE_ROOT, BACKUP_ROOT)._check_source()

    if (TARGET_SCRIPT.exists() or TARGET_SCRIPT.is_symlink()
            or BACKUP_ROOT.exists() or BACKUP_ROOT.is_symlink()
            or DROPIN_DIR.exists() or DROPIN_DIR.is_symlink()):
        raise RuntimeError("diagnostic backup targets already occupied")
    info = os.statvfs(BACKUP_ROOT.parent)
    if info.f_bavail * info.f_frsize < 1024 * 1024 * 1024:
        raise RuntimeError("insufficient disk space for private backup")

    created_script = False
    created_dropin = False
    try:
        _new_file(TARGET_SCRIPT, script_content, 0o755)
        created_script = True
        BACKUP_ROOT.mkdir(mode=0o700)
        os.chown(BACKUP_ROOT, uid.pw_uid, uid.pw_gid)
        _private_root(BACKUP_ROOT, uid.pw_uid, mode=0o700)
        DROPIN_DIR.mkdir(mode=0o755)
        DROPIN_DIR.chmod(0o755)
        _new_file(DROPIN, dropin_bytes(), 0o644)
        created_dropin = True
        _command("/usr/bin/systemd-analyze", "verify", str(BASE_UNIT))
        _command("/usr/bin/systemctl", "daemon-reload")
        effective = subprocess.check_output(
            ("/usr/bin/systemctl", "show", "-p", "ExecStartPost", "ae-backup.service"),
            text=True, timeout=10)
        if str(TARGET_SCRIPT) not in effective:
            raise RuntimeError("existing backup timer did not load diagnostic post-step")
        _baseline(expected)
        with _as_runtime_user(uid.pw_uid, uid.pw_gid):
            backup = DiagnosticBackup(SOURCE_ROOT, BACKUP_ROOT)
            name, retained = backup.create()
            backup.verify(BACKUP_ROOT / name)
            result = backup.restore_smoke(BACKUP_ROOT / name)
        _baseline(expected)
        report = {"status": "DIAGNOSTIC_BACKUP_ENABLED", "scope": "vl-ktk/ai-agent-engine",
                  "snapshot": name, "retained": retained,
                  "restore": result["status"], "schedule": "existing_ae-backup.timer",
                  "existing_backup_executable_changed": False, "diagnostic_service_activated": False,
                  "live_release": expected}
        (evidence / "diagnostic-backup-7-7.json").write_text(
            json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n")
        print("DIAG_BACKUP_7_7 " + json.dumps(report, sort_keys=True,
                                             separators=(",", ":")), flush=True)
    except Exception:
        # Disable only files this run just created; preserve new backup data.
        if created_dropin and DROPIN.exists() and not DROPIN.is_symlink():
            shutil.move(str(DROPIN), str(evidence / DROPIN.name))
        if created_script and TARGET_SCRIPT.exists() and not TARGET_SCRIPT.is_symlink():
            shutil.move(str(TARGET_SCRIPT), str(evidence / TARGET_SCRIPT.name))
        subprocess.run(("/usr/bin/systemctl", "daemon-reload"),
                       check=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        raise


if __name__ == "__main__":
    main()
