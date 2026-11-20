"""One-time owner-gated, isolated diagnostic task-owner service activation.

Installs a separate pinned Python code tree and one systemd unit. The live
orchestrator release, backups, three baseline units, project source, existing
task/registry data and model configuration are never replaced or migrated.
On failure, only newly installed code/unit are disabled and moved to private
owner evidence; existing private databases and backup snapshots are retained.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time


LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
PROJECT_SHA = "741fc1a4e62b52922611e1e28d4805d727dffe4d"
CURRENT = Path("/opt/ai-agent-engine/current")
ROOT = Path("/opt/ai-agent-engine/diagnostic-7-7")
CODE = ROOT / "code"
LAUNCHER = ROOT / "launcher.py"
UNIT = Path("/etc/systemd/system/ae-managed-diagnostics.service")
UNIT_NAME = "ae-managed-diagnostics.service"
PRIVATE_ROOT = Path("/var/lib/ai-agent-engine-diagnostics")
BACKUP_ROOT = Path("/var/backups/ai-agent-engine-diagnostics")
SOCKET = Path("/run/ai-agent-engine-diagnostics/intake.sock")
BACKUP_EXEC = Path("/usr/local/libexec/ae-diagnostic-backup.py")
BACKUP_DROPIN = Path("/etc/systemd/system/ae-backup.service.d/70-diagnostic-project.conf")
BACKUP_SHA = "9930b6d4d34d751cc661fe7e20c2527508715056e35139d7a59ea2db554cdb5a"
BACKUP_DROPIN_TEXT = ("[Service]\n"
                      "ReadOnlyPaths=/var/lib/ai-agent-engine-diagnostics\n"
                      "ReadWritePaths=/var/backups/ai-agent-engine-diagnostics\n"
                      "ExecStartPost=/usr/bin/python3 /usr/local/libexec/ae-diagnostic-backup.py create\n")
RUNTIME_SHA = "dcf077701d2fd92fe6cef5cebcd8966e0fd3217c7c8c5a88eb3de447ec4e769b"
SERVICES = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def unit_text() -> str:
    return f"""[Unit]
Description=AI Agent Engine single-owner private diagnostics (7.7)
After=local-fs.target
ConditionPathExists={PRIVATE_ROOT}/runtime.json

[Service]
Type=simple
User=ae-runtime
Group=ae-runtime
WorkingDirectory=/
ExecStart=/usr/bin/python3 -I {LAUNCHER} --config {PRIVATE_ROOT}/runtime.json
Restart=no
TimeoutStopSec=15
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
RuntimeDirectory=ai-agent-engine-diagnostics
RuntimeDirectoryMode=0700
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictAddressFamilies=AF_UNIX
LockPersonality=yes
MemoryMax=192M
TasksMax=12
CPUQuota=20%
ReadWritePaths={PRIVATE_ROOT} /run/ai-agent-engine-diagnostics
StandardOutput=journal
StandardError=journal
LogRateLimitIntervalSec=30s
LogRateLimitBurst=30

[Install]
WantedBy=multi-user.target
"""


def launcher_text() -> str:
    return ("\"\"\"Pinned private stage-7.7 entry; independent of live package.\"\"\"\n"
            "import sys\n"
            f"sys.path.insert(0, {str(CODE)!r})\n"
            "from ai_agent_engine.diagnostic_managed_runtime import main\n"
            "raise SystemExit(main())\n")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _private_dir(path: Path, uid: int) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("unsafe_private_directory")


def _regular(path: Path, uid: int, mode: int) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeError("unsafe_reviewed_file")


def _command(*args: str, timeout: int = 30) -> None:
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=timeout)


def _systemctl(*args: str) -> None:
    _command("/usr/bin/systemctl", *args)


@contextmanager
def _as_user(uid: int, gid: int):
    old_uid, old_gid = os.geteuid(), os.getegid()
    os.setegid(gid)
    os.seteuid(uid)
    try:
        yield
    finally:
        os.seteuid(old_uid)
        os.setegid(old_gid)


def _baseline(expected: str) -> None:
    if (expected != LIVE_SHA or os.readlink(CURRENT) != "releases/" + expected):
        raise RuntimeError("live_release_changed")
    for unit in SERVICES:
        _systemctl("is-active", "--quiet", unit)
    _regular(BACKUP_EXEC, 0, 0o755)
    _regular(BACKUP_DROPIN, 0, 0o644)
    if (_hash(BACKUP_EXEC) != BACKUP_SHA
            or BACKUP_DROPIN.read_text() != BACKUP_DROPIN_TEXT):
        raise RuntimeError("diagnostic_backup_changed")


def _reviewed_source() -> tuple[Path, list[Path]]:
    source = Path.cwd() / "ai_agent_engine"
    runtime = source / "diagnostic_managed_runtime.py"
    if (source.is_symlink() or not source.is_dir() or runtime.is_symlink()
            or _hash(runtime) != RUNTIME_SHA):
        raise RuntimeError("runtime_source_not_reviewed")
    files = sorted(source.glob("*.py"))
    if (len(files) < 20 or not (source / "__init__.py").is_file()
            or any(not f.is_file() or f.is_symlink() for f in files)):
        raise RuntimeError("unsafe_runtime_package")
    return source, files


def _exclusive_file(path: Path, content: bytes, mode: int) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _check_preprovisioned(uid: int, gid: int) -> None:
    _private_dir(PRIVATE_ROOT, uid)
    _private_dir(BACKUP_ROOT, uid)
    # The systemd-managed runtime directory is intentionally still absent.
    sys.path.insert(0, str(Path.cwd()))
    from ai_agent_engine.diagnostic_host_preflight import inspect_preprovisioned_host
    from ai_agent_engine.diagnostic_intake_service import _private_config
    with _as_user(uid, gid):
        doc = _private_config(PRIVATE_ROOT / "runtime.json")
        report = inspect_preprovisioned_host(PRIVATE_ROOT / "runtime.json")
    if (doc["socket_path"] != str(SOCKET)
            or report.source_commit != PROJECT_SHA
            or report.candidate_ready
            or report.blockers != ("private_host_directories",)):
        raise RuntimeError("preprovisioned_host_state_changed")


def _active_socket_probe(uid: int, gid: int) -> int:
    _systemctl("is-active", "--quiet", UNIT_NAME)
    info = SOCKET.lstat()
    _private_dir(SOCKET.parent, uid)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError("unsafe_diagnostic_socket")
    with _as_user(uid, gid):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(SOCKET))
            client.sendall(b"{}\n")  # intentionally invalid: no synthetic incident
            response = client.recv(256)
    if json.loads(response) != {"ok": False, "status": "HOLD"}:
        raise RuntimeError("unexpected_diagnostic_socket_response")
    # Only the assigned service's MainPID is accepted as the single task owner.
    result = subprocess.check_output(
        ("/usr/bin/systemctl", "show", "-p", "MainPID", "--value", UNIT_NAME),
        text=True, timeout=10).strip()
    if not result.isdigit() or int(result) < 2:
        raise RuntimeError("managed_process_pid_missing")
    pid = int(result)
    status = Path(f"/proc/{pid}/status").read_text()
    if not any(line.startswith(f"Uid:\t{uid}\t") for line in status.splitlines()):
        raise RuntimeError("managed_process_uid_mismatch")
    if str(LAUNCHER).encode() not in Path(f"/proc/{pid}/cmdline").read_bytes():
        raise RuntimeError("managed_process_command_mismatch")
    # Existing empty project task DB remains empty after starting the socket.
    with _as_user(uid, gid):
        uri = f"file:{PRIVATE_ROOT / 'tasks.sqlite3'}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as db:
            if db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] != 0:
                raise RuntimeError("unexpected_task_record")
    return pid


def _wait_socket(uid: int, gid: int) -> int:
    deadline = time.monotonic() + 9
    while time.monotonic() < deadline:
        if SOCKET.exists():
            return _active_socket_probe(uid, gid)
        time.sleep(0.1)
    raise RuntimeError("managed_socket_not_ready")


def main() -> None:
    if os.geteuid() != 0 or not all(os.environ.get(k) for k in
                                   ("AE_EXPECTED_CURRENT", "AE_REQUEST_SHA", "AE_SOURCE_SHA", "AE_EVIDENCE")):
        raise RuntimeError("authenticated_owner_admin_channel_required")
    expected = os.environ["AE_EXPECTED_CURRENT"]
    evidence = Path(os.environ["AE_EVIDENCE"])
    _private_dir(evidence, 0)
    user = pwd.getpwnam("ae-runtime")
    uid, gid = user.pw_uid, user.pw_gid
    _baseline(expected)
    _source, files = _reviewed_source()
    _check_preprovisioned(uid, gid)
    if any(p.exists() or p.is_symlink() for p in (ROOT, UNIT, SOCKET)):
        raise RuntimeError("managed_runtime_target_occupied")
    # Re-check deployed release immediately before the first host mutation.
    _baseline(expected)
    created_code = False
    created_unit = False
    try:
        # Mark ownership before installation so partial directories are kept
        # for recovery even if a later copy fails mid-file.
        ROOT.mkdir(mode=0o755)
        created_code = True
        # The helper creates the nested dirs; parent itself is already new.
        CODE.mkdir(mode=0o755)
        package_dir = CODE / "ai_agent_engine"
        package_dir.mkdir(mode=0o755)
        manifest: dict[str, str] = {}
        for source in files:
            content = source.read_bytes()
            target = package_dir / source.name
            _exclusive_file(target, content, 0o444)
            manifest[source.name] = hashlib.sha256(content).hexdigest()
            if _hash(target) != manifest[source.name]:
                raise RuntimeError("installed_source_digest_mismatch")
        _exclusive_file(LAUNCHER, launcher_text().encode(), 0o444)
        _exclusive_file(UNIT, unit_text().encode(), 0o644)
        created_unit = True
        _command("/usr/bin/systemd-analyze", "verify", str(UNIT))
        _systemctl("daemon-reload")
        _baseline(expected)
        _systemctl("enable", "--now", UNIT_NAME)
        first_pid = _wait_socket(uid, gid)
        _systemctl("restart", UNIT_NAME)
        second_pid = _wait_socket(uid, gid)
        if second_pid == first_pid:
            raise RuntimeError("managed_process_was_not_restarted")
        _baseline(expected)
        report = {
            "status": "MANAGED_DIAGNOSTICS_ACTIVE", "scope": "vl-ktk/ai-agent-engine",
            "project_source": PROJECT_SHA, "code_files": len(manifest),
            "source_manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            "live_release": expected, "live_release_changed": False,
            "restart_probe": "PASS", "socket_probe": "HOLD_EXPECTED",
            "no_tasks_or_grants_created": True, "baseline_services_unchanged": True,
            "old_controller_restarted": False, "model_or_network_enabled": False,
            "main_pid_verified": second_pid > 1 and first_pid > 1,
        }
        _exclusive_file(evidence / "managed-diagnostics-7-7.json",
                        (json.dumps(report, sort_keys=True) + "\n").encode(), 0o600)
        print("MANAGED_DIAGNOSTICS_7_7 " + json.dumps(report, sort_keys=True), flush=True)
    except Exception:
        # Stop/disable *only* our newly installed unit. Keep all project DBs,
        # existing backup snapshots, current live release and old services.
        if created_unit:
            subprocess.run(("/usr/bin/systemctl", "disable", "--now", UNIT_NAME),
                           capture_output=True, check=False, timeout=30)
            if UNIT.exists() and not UNIT.is_symlink():
                shutil.move(str(UNIT), str(evidence / UNIT.name))
        if created_code and ROOT.exists() and not ROOT.is_symlink():
            shutil.move(str(ROOT), str(evidence / "diagnostic-7-7-code"))
        subprocess.run(("/usr/bin/systemctl", "daemon-reload"),
                       capture_output=True, check=False, timeout=30)
        raise


if __name__ == "__main__":
    main()
