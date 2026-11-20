"""Signed, isolated 7.7 v2 upgrade and exactly one owner-confirmed QA run.

Invoked only by the replacement root-pinned signed operator, from its
independently verified wheel source. Never touches the live controller or
other projects. Any unknown task/process state is held for owner review; the
project databases are never restored, rewritten or deleted on failure.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess
import sys
import time


LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
SOURCE_SHA = "3ef07c51ec287a24bd54d8fcea7c40a99e69b569"
PROJECT_SHA = "741fc1a4e62b52922611e1e28d4805d727dffe4d"
OLD_RUNTIME_SHA = "dcf077701d2fd92fe6cef5cebcd8966e0fd3217c7c8c5a88eb3de447ec4e769b"
NEW_RUNTIME_SHA = "875206bcae8f3d8f410a0d254317025857718094bab760dcf8d80da44385fd19"
NEGATIVE_QA_PROPOSAL_SHA = "d31293ad8f5b077c23542d2855b3491edcd10dc44a0e283ed6c50da7c3210510"
OLD_UNIT_SHA = "1afa538cf507f56dfb8905cd444dd52a4c282c8a00c2a52ea1a22088bf41e0e1"
CURRENT = Path("/opt/ai-agent-engine/current")
OLD_ROOT = Path("/opt/ai-agent-engine/diagnostic-7-7")
ROOT = Path("/opt/ai-agent-engine/diagnostic-7-7-v2")
CODE = ROOT / "code"
LAUNCHER = ROOT / "launcher.py"
QA_LAUNCHER = ROOT / "qa-once.py"
MAIN_UNIT = Path("/etc/systemd/system/ae-managed-diagnostics.service")
QA_UNIT = Path("/etc/systemd/system/ae-managed-diagnostics-qa.service")
MAIN_NAME = MAIN_UNIT.name
QA_NAME = QA_UNIT.name
PRIVATE_ROOT = Path("/var/lib/ai-agent-engine-diagnostics")
SOCKET = Path("/run/ai-agent-engine-diagnostics/intake.sock")
BASELINE = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _safe_regular(path: Path, *, uid: int, mode: int) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeError("untrusted_existing_file")


def _safe_directory(path: Path, *, uid: int, mode: int) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeError("untrusted_existing_directory")


def _run(*args: str, timeout: int = 20) -> None:
    subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                   check=True, timeout=timeout)


def _systemctl(*args: str, timeout: int = 20) -> None:
    _run("/usr/bin/systemctl", *args, timeout=timeout)


def _baseline() -> None:
    if (not CURRENT.is_symlink()
            or os.readlink(CURRENT) != "releases/" + LIVE_SHA):
        raise RuntimeError("baseline_controller_changed")
    for unit in BASELINE:
        _systemctl("is-active", "--quiet", unit)


def _exclusive(path: Path, body: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode)
    try:
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    _safe_regular(path, uid=0, mode=mode)
    if _hash(path) != sha256(body).hexdigest():
        raise RuntimeError("staged_code_changed")


def main_unit_text() -> str:
    return f"""[Unit]
Description=AI Agent Engine single-owner private diagnostics (7.7 v2)
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


def qa_unit_text() -> str:
    original = main_unit_text()
    return (original.replace("Description=AI Agent Engine single-owner private diagnostics (7.7 v2)",
                             "Description=AI Agent Engine signed owner one-task QA (7.7 v2)")
            .replace("Type=simple", "Type=oneshot")
            .replace(f"ExecStart=/usr/bin/python3 -I {LAUNCHER} --config {PRIVATE_ROOT}/runtime.json",
                     f"ExecStart=/usr/bin/python3 -I {QA_LAUNCHER}")
            .replace("TimeoutStopSec=15", "TimeoutStopSec=20\nTimeoutStartSec=90")
            .replace("[Install]\nWantedBy=multi-user.target\n", ""))


def launcher_text() -> str:
    return ("import sys\n"
            f"sys.path.insert(0, {str(CODE)!r})\n"
            "from ai_agent_engine.diagnostic_managed_runtime import main\n"
            "raise SystemExit(main())\n")


def qa_launcher_text() -> str:
    # One immutable owner-signed operation; no socket/CLI/worker-controlled
    # argument can choose another task, grant, mode or target project.
    proposal_id = "proposal_" + SOURCE_SHA[:32]
    return ("import sys\n"
            "from pathlib import Path\n"
            "from threading import Event\n"
            f"sys.path.insert(0, {str(CODE)!r})\n"
            "from ai_agent_engine.task_api import OwnerApprovalProof\n"
            "from ai_agent_engine.diagnostic_managed_runtime import (\n"
            "    OwnerConfirmedDiagnosticQA, QA_NEGATIVE_PROPOSAL_SHA256, run_managed)\n"
            f"proof = OwnerApprovalProof({proposal_id!r}, 'message', QA_NEGATIVE_PROPOSAL_SHA256)\n"
            "stop = Event()\n"
            "stop.set()\n"
            f"run_managed(Path({str(PRIVATE_ROOT / 'runtime.json')!r}), stop, "
            "qa=OwnerConfirmedDiagnosticQA(proof, negative_check=True))\n")


def _verify_pre_upgrade(uid: int) -> None:
    _baseline()
    _systemctl("is-active", "--quiet", MAIN_NAME)
    _safe_regular(MAIN_UNIT, uid=0, mode=0o644)
    if _hash(MAIN_UNIT) != OLD_UNIT_SHA:
        raise RuntimeError("installed_unit_changed")
    _safe_regular(OLD_ROOT / "code/ai_agent_engine/diagnostic_managed_runtime.py",
                  uid=0, mode=0o444)
    if _hash(OLD_ROOT / "code/ai_agent_engine/diagnostic_managed_runtime.py") != OLD_RUNTIME_SHA:
        raise RuntimeError("installed_runtime_changed")
    _safe_directory(PRIVATE_ROOT, uid=uid, mode=0o700)
    _safe_regular(PRIVATE_ROOT / "runtime.json", uid=uid, mode=0o600)
    for path in (ROOT, QA_UNIT):
        if path.exists() or path.is_symlink():
            raise RuntimeError("v2_target_already_exists")


def _source_files() -> list[Path]:
    folder = Path.cwd() / "ai_agent_engine"
    files = sorted(folder.glob("*.py"))
    if (folder.is_symlink() or len(files) < 50
            or any(f.is_symlink() or not f.is_file() for f in files)
            or _hash(folder / "diagnostic_managed_runtime.py") != NEW_RUNTIME_SHA):
        raise RuntimeError("reviewed_candidate_changed")
    return files


def _verified_host_qa(uid: int) -> dict[str, object]:
    """Read only the one exact approved task after the transient unit exits."""
    db_path = PRIVATE_ROOT / "tasks.sqlite3"
    _safe_regular(db_path, uid=uid, mode=0o600)
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        tasks = db.execute("SELECT task_id,owner_id,project_id,title FROM tasks").fetchall()
        if len(tasks) != 1 or tasks[0][1:] != (
                "vl-ktk", "ai-agent-engine", "7.7 explicitly expected negative QA"):
            raise RuntimeError("unexpected_qa_task_scope")
        task_id = tasks[0][0]
        rows = db.execute("SELECT state FROM task_states WHERE task_id=?", (task_id,)).fetchall()
        perms = db.execute("SELECT action,allowed FROM task_permission_audit WHERE task_id=?",
                           (task_id,)).fetchall()
        deliveries = db.execute("SELECT status FROM task_deliveries WHERE task_id=?",
                                (task_id,)).fetchall()
        owner_receipts = db.execute("SELECT owner_id,project_id,proposal_id,channel,proposal_sha256 "
                                    "FROM task_owner_approvals WHERE task_id=?", (task_id,)).fetchall()
        if (rows != [("failed",)] or perms != [("execute_tool", 1)]
                or deliveries != [("exhausted",)]
                or owner_receipts != [("vl-ktk", "ai-agent-engine",
                                       "proposal_" + SOURCE_SHA[:32], "message",
                                       NEGATIVE_QA_PROPOSAL_SHA)]):
            raise RuntimeError("qa_task_execution_not_proven")
    incident_path = (PRIVATE_ROOT / "projects/vl-ktk/ai-agent-engine"
                     / "diagnostics_7_7.sqlite3")
    _safe_regular(incident_path, uid=uid, mode=0o600)
    with sqlite3.connect(f"file:{incident_path}?mode=ro", uri=True, timeout=5) as db:
        incidents = db.execute("SELECT incident_id,task_id,status FROM incidents").fetchall()
        if len(incidents) != 1 or incidents[0][1:] != (task_id, "OPEN"):
            raise RuntimeError("real_qa_incident_not_durable")
        occurrences = db.execute("SELECT error_code,check_name FROM occurrences WHERE incident_id=?",
                                 (incidents[0][0],)).fetchall()
        if occurrences != [("ExpectedQaMissingMarker", "git_owner_qa_missing_marker")]:
            raise RuntimeError("qa_failure_provenance_mismatch")
    return {"qa_task_id": task_id, "qa_incident_id": incidents[0][0],
            "status": "SIGNED_REAL_SUBPROCESS_QA_FAILED_AS_EXPECTED",
            "qa_outcome": "EXPECTED_NEGATIVE", "source": SOURCE_SHA}


def _wait_socket(uid: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if SOCKET.exists():
            _safe_directory(SOCKET.parent, uid=uid, mode=0o700)
            info = SOCKET.lstat()
            if (stat.S_ISSOCK(info.st_mode) and info.st_uid == uid
                    and stat.S_IMODE(info.st_mode) == 0o600):
                _systemctl("is-active", "--quiet", MAIN_NAME)
                return
        time.sleep(0.15)
    raise RuntimeError("new_diagnostic_service_socket_missing")


def main() -> None:
    if (os.geteuid() != 0 or os.environ.get("AE_EXPECTED_CURRENT") != LIVE_SHA
            or os.environ.get("AE_SOURCE_SHA") != SOURCE_SHA
            or not os.environ.get("AE_REQUEST_SHA")):
        raise RuntimeError("signed_owner_upgrade_gate_required")
    evidence = Path(os.environ["AE_EVIDENCE"])
    _safe_directory(evidence, uid=0, mode=0o700)
    uid = pwd.getpwnam("ae-runtime").pw_uid
    _verify_pre_upgrade(uid)
    files = _source_files()
    _baseline()  # recheck immediately before any host change
    created_root = False
    created_qa_unit = False
    stopped_old = False
    unit_swapped = False
    try:
        ROOT.mkdir(mode=0o755)
        ROOT.chmod(0o755)
        created_root = True
        CODE.mkdir(mode=0o755)
        CODE.chmod(0o755)
        package = CODE / "ai_agent_engine"
        package.mkdir(mode=0o755)
        package.chmod(0o755)
        for source in files:
            _exclusive(package / source.name, source.read_bytes(), 0o444)
        _exclusive(LAUNCHER, launcher_text().encode(), 0o444)
        _exclusive(QA_LAUNCHER, qa_launcher_text().encode(), 0o444)
        _exclusive(QA_UNIT, qa_unit_text().encode(), 0o644)
        created_qa_unit = True
        _run("/usr/bin/systemd-analyze", "verify", str(QA_UNIT))
        _systemctl("daemon-reload")
        _baseline()
        _systemctl("stop", MAIN_NAME)
        stopped_old = True
        if SOCKET.exists() or SOCKET.is_symlink():
            raise RuntimeError("old_diagnostic_socket_remains")
        # The QA unit is the sole owner of the private task DB while active.
        _systemctl("start", QA_NAME, timeout=100)
        report = _verified_host_qa(uid)
        _baseline()
        original = MAIN_UNIT.read_bytes()
        if sha256(original).hexdigest() != OLD_UNIT_SHA:
            raise RuntimeError("old_unit_changed_during_qa")
        _exclusive(evidence / "old-managed-unit.service", original, 0o600)
        replacement = evidence / "staged-managed-v2.service"
        _exclusive(replacement, main_unit_text().encode(), 0o600)
        os.chmod(replacement, 0o644)
        os.replace(replacement, MAIN_UNIT)
        unit_swapped = True
        _systemctl("daemon-reload")
        _systemctl("start", MAIN_NAME)
        _wait_socket(uid)
        _baseline()
        report.update({"runtime_service_active": True,
                       "old_controller_unchanged": True,
                       "old_diagnostic_code_preserved": True,
                       "backup_timers_unchanged": True})
        _exclusive(evidence / "diagnostic-v2-signed-qa.json",
                   (json.dumps(report, sort_keys=True) + "\n").encode(), 0o600)
        print("SIGNED_DIAGNOSTICS_V2 " + json.dumps(report, sort_keys=True), flush=True)
    except Exception:
        if unit_swapped:
            subprocess.run(("/usr/bin/systemctl", "stop", MAIN_NAME),
                           capture_output=True, check=False, timeout=20)
            backup = evidence / "old-managed-unit.service"
            if backup.is_file() and _hash(backup) == OLD_UNIT_SHA:
                staged = MAIN_UNIT.with_name(".ae-managed-diagnostics-v1.rollback")
                _exclusive(staged, backup.read_bytes(), 0o644)
                os.replace(staged, MAIN_UNIT)
                subprocess.run(("/usr/bin/systemctl", "daemon-reload"),
                               capture_output=True, check=False, timeout=20)
        # Never restore the existing private SQLite files: a child may have
        # run. Unknown ACTIVE/RUNNING work stays owner-review HOLD, no replay.
        if stopped_old:
            print("SIGNED_DIAGNOSTICS_V2 HOLD: old diagnostics stopped; task state needs owner review", flush=True)
        else:
            print("SIGNED_DIAGNOSTICS_V2 HOLD: pre-live staging rejected", flush=True)
        raise


if __name__ == "__main__":
    main()
