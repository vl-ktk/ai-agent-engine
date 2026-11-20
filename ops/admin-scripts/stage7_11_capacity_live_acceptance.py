"""Owner-gated Netcup 7.11 E2E using existing subscription sign-ins.

The reviewed current source is copied into disposable private scratch. The
already-installed SIWC Python and official Claude binary are used; no core
release, service, working task DB, provider configuration or API key changes.
One normal SIWC refresh may rotate the owner's existing OAuth token. No login
is invented, no responses or credentials are printed, and no failed call is
retried. Root only orchestrates a process running as the actual owner user.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile


EXPECTED_RELEASE = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
EXPECTED_HOOK = "62ef63e2c9fca437ce2ab0944a6f677258001011ea1045422a460d0c59d9ddc0"
CURRENT = Path("/opt/ai-agent-engine/current")
PYTHON = Path("/opt/ai-agent-engine/owner-siwc-6-14/venv/bin/python")
CLAUDE = Path("/usr/local/bin/claude")
HOOK = Path("/usr/local/libexec/ae-diagnostic-backup.py")
TASKS = Path("/var/lib/ai-agent-engine-diagnostics/tasks.sqlite3")
SOURCE = Path("ai_agent_engine")
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def _process(argv: list[str], *, timeout: int = 30,
             cwd: Path | None = None) -> tuple[bool, bytes]:
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=cwd, start_new_session=True)
    except OSError:
        return False, b""
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Stop the entire owner-command process group on a preflight timeout.
        # Provider children additionally have their independent controller
        # watchdog/process ledger, never a guessed PID retry.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        return False, b""
    return process.returncode == 0, output[:8192]


def _private_regular(path: Path, *, owner: int, mode: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == owner
            and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1)


def _db_identity(uid: int) -> tuple[int, int, int, str]:
    if not _private_regular(TASKS, owner=uid, mode=0o600):
        raise RuntimeError("private live task SQLite changed")
    if any(Path(str(TASKS) + suffix).exists()
           for suffix in ("-wal", "-shm", "-journal")):
        raise RuntimeError("working task SQLite is not quiescent")
    details = TASKS.stat()
    with sqlite3.connect("file:" + str(TASKS) + "?mode=ro&immutable=1",
                         uri=True) as db:
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        app = db.execute("PRAGMA application_id").fetchone()[0]
        integrity = db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    if schema != 14 or app != 0x4145494E or not integrity:
        raise RuntimeError("live task SQLite baseline changed")
    # Hash only for the local before/after comparison; never disclose it.
    digest = hashlib.sha256(TASKS.read_bytes()).hexdigest()
    return (details.st_size, details.st_mtime_ns, schema, digest)


def _baseline(uid: int) -> tuple[int, int, int, str]:
    if os.readlink(CURRENT) != "releases/" + EXPECTED_RELEASE:
        raise RuntimeError("production core release changed")
    for unit in UNITS:
        if not _process(["/usr/bin/systemctl", "is-active", "--quiet", unit],
                        timeout=8)[0]:
            raise RuntimeError("production unit unhealthy")
    if not _private_regular(HOOK, owner=0, mode=0o755):
        raise RuntimeError("approved backup hook metadata changed")
    if hashlib.sha256(HOOK.read_bytes()).hexdigest() != EXPECTED_HOOK:
        raise RuntimeError("approved backup hook content changed")
    return _db_identity(uid)


def _owner_command(runtime, argv: list[str], *, timeout: int = 30,
                   source: Path | None = None) -> tuple[bool, bytes]:
    home = runtime.pw_dir
    environment = [
        "/usr/bin/env", "-i", "HOME=" + home,
        "USER=ae-runtime", "LOGNAME=ae-runtime", "LANG=C.UTF-8",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE=1", "PYTHONNOUSERSITE=1",
    ]
    if source is not None:
        environment.append("PYTHONPATH=" + str(source))
    return _process(["/usr/sbin/runuser", "-u", "ae-runtime", "--",
                     *environment, *argv], timeout=timeout, cwd=source)


def _authorize_existing_sessions(runtime) -> bool:
    """Refresh only the existing owner's SIWC grant; never create a login."""
    if not PYTHON.is_file() or not CLAUDE.is_file():
        raise RuntimeError("existing owner provider executables unavailable")
    code = ("from ai_agent_engine.session_refresh import refresh_if_needed;"
            "refresh_if_needed()")
    if not _owner_command(runtime, [str(PYTHON), "-I", "-c", code],
                          timeout=45)[0]:
        raise RuntimeError("existing GPT OAuth refresh blocked; owner login needed")
    gpt_ok, gpt_out = _owner_command(runtime, [str(PYTHON), "-I", "-m",
        "ai_agent_engine.auth", "status"])
    if (not gpt_ok or b"Saved ChatGPT identity:" not in gpt_out
            or b"ChatGPT plan permission: GRANTED" not in gpt_out
            or b"Access token expired." in gpt_out):
        raise RuntimeError("GPT plan permission not ready after refresh")
    model_code = ("from ai_agent_engine.inference import list_models;"
                  "import sys;"
                  "sys.exit(0 if 'gpt-6.1-sol' in {name for name,_ in list_models()} else 4)")
    if not _owner_command(runtime, [str(PYTHON), "-I", "-c", model_code],
                          timeout=65)[0]:
        raise RuntimeError("approved GPT model unavailable; no model call attempted")
    claude_ok, claude_out = _owner_command(runtime,
        [str(CLAUDE), "auth", "status"], timeout=30)
    try:
        info = json.loads(claude_out) if claude_ok else None
    except (TypeError, ValueError, UnicodeError):
        info = None
    if (not isinstance(info, dict) or info.get("loggedIn") is not True
            or info.get("authMethod") != "claude.ai"
            or info.get("apiProvider") not in (None, "firstParty")
            or info.get("subscriptionType") not in ("pro", "max")):
        raise RuntimeError("personal Claude subscription not ready")
    return True


def _prepare_disposable_source(directory: Path, *, owner_uid: int,
                               owner_gid: int) -> Path:
    if (type(owner_uid) is not int or owner_uid <= 0
            or type(owner_gid) is not int or owner_gid < 0):
        raise RuntimeError("non-root disposable runtime identity required")
    if not SOURCE.is_dir() or SOURCE.is_symlink():
        raise RuntimeError("reviewed source package missing")
    if any(path.is_symlink() for path in SOURCE.rglob("*")):
        raise RuntimeError("reviewed source package has unexpected symlink")
    package = directory / "ai_agent_engine"
    shutil.copytree(SOURCE, package)
    # Root extracts the immutable payload under a private umask. copytree
    # preserves its root-only modes and creates root-owned files. Chown ONLY
    # the newly copied disposable package so the non-root runner can import
    # it; never change the source payload, live release, home or task DB.
    for path in package.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise RuntimeError("disposable source has unexpected file type")
        os.chown(path, owner_uid, owner_gid, follow_symlinks=False)
    os.chown(package, owner_uid, owner_gid, follow_symlinks=False)
    return directory


def _status(data: bytes, expected: str) -> bool:
    for line in data.splitlines():
        if not line.startswith(b"STAGE7_11_OWNER_E2E "):
            continue
        try:
            receipt = json.loads(line.split(b" ", 1)[1])
        except (UnicodeError, ValueError, TypeError):
            return False
        return (type(receipt) is dict
                and receipt.get("status") == expected
                and receipt.get("providers") == ["gpt", "claude"]
                and receipt.get("cpu_slot_wait") == "PASS"
                and receipt.get("provider_quota_wait") == "PASS"
                and receipt.get("delivery_not_spent_on_wait") is True
                and receipt.get("durable_sqlite_v17") is True
                and receipt.get("live_provider_calls") ==
                (2 if expected == "LIVE_PASS" else 0))
    return False


def main() -> None:
    if (os.geteuid() != 0 or os.environ.get("AE_EXPECTED_CURRENT") != EXPECTED_RELEASE
            or not all(os.environ.get(name) for name in
                       ("AE_SOURCE_SHA", "AE_REQUEST_SHA", "AE_REQUEST_ID", "AE_EVIDENCE"))):
        raise RuntimeError("authenticated owner-admin request required")
    runtime = pwd.getpwnam("ae-runtime")
    before = _baseline(runtime.pw_uid)
    evidence = Path(os.environ["AE_EVIDENCE"])
    info = evidence.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("owner evidence directory unsafe")
    # Do not start models until the pre-existing personal logins are ready.
    _authorize_existing_sessions(runtime)
    if _baseline(runtime.pw_uid) != before:
        raise RuntimeError("production changed during provider preflight")

    with tempfile.TemporaryDirectory(prefix="ae-711-disposable-", dir="/var/tmp") as tmp:
        directory = Path(tmp)
        os.chown(directory, runtime.pw_uid, runtime.pw_gid)
        source = _prepare_disposable_source(
            directory, owner_uid=runtime.pw_uid, owner_gid=runtime.pw_gid)
        command = [str(PYTHON), "-m", "ai_agent_engine.task_stage7_11_acceptance"]
        offline_ok, offline = _owner_command(runtime, command,
                                             source=source, timeout=85)
        if not (offline_ok and _status(offline, "OFFLINE_PASS")):
            raise RuntimeError("stage 7.11 disposable offline acceptance blocked")
        if _baseline(runtime.pw_uid) != before:
            raise RuntimeError("production changed before live calls")
        # This is the *one* owner-approved live invocation. No retries.
        live_ok, live = _owner_command(runtime,
            [*command, "--live", "--owner-approved"], source=source,
            timeout=475)
        after = _baseline(runtime.pw_uid)
        passed = live_ok and _status(live, "LIVE_PASS") and after == before
        report = {
            "schema": 1, "scope": "vl-ktk/ai-agent-engine",
            "live_release": EXPECTED_RELEASE,
            "status": "LIVE_PASS" if passed else "LIVE_BLOCKED",
            "gpt_existing_siwc_refreshed_or_still_valid": True,
            "claude_personal_login_valid": True,
            "offline_capacity_e2e": "PASS",
            "live_capacity_e2e": "PASS" if passed else "BLOCKED",
            "live_invocation_once": True, "automatic_retry": False,
            "max_live_provider_calls": 2,
            "temporary_task_sqlite_v17": True,
            "working_task_sqlite_unchanged": after == before,
            "production_release_unchanged": True,
            "credentials_printed": False, "provider_output_printed": False,
            "paid_api_fallback": False, "new_services_installed": False,
        }
        with (evidence / "stage7-11-capacity-e2e.json").open("x", encoding="utf-8") as f:
            json.dump(report, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
        print("STAGE7_11_NETCUP_E2E " + json.dumps(report, sort_keys=True,
                                                     separators=(",", ":")), flush=True)
        if not passed:
            raise RuntimeError("stage 7.11 live acceptance blocked; no retry")


if __name__ == "__main__":
    main()
