#!/usr/bin/env python3
"""One-shot narrowly scoped fix for root-umask on the NEW Vault code only.

The first source-approved installer created Vault safely but its 077 umask
also made public Python *source directories* 0700. This script changes only
four fixed root-owned paths under /opt. It never modifies Vault data, keys,
credentials, controller or any other project. It then verifies live health.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time


BASELINE = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
VAULT_SOURCE = "38d0e960d63f725ee5d5de5fbf71c8e03052ce52"
VAULT_CODE_ROOT = Path("/opt/ai-agent-engine-vault")
RELEASE = VAULT_CODE_ROOT / "releases" / VAULT_SOURCE
SOCKET = Path("/run/ae-vault.sock")
BACKUP = Path("/var/backups/ai-agent-engine-vault")


def _check_controller() -> None:
    if (os.environ.get("AE_EXPECTED_CURRENT") != BASELINE
            or os.readlink("/opt/ai-agent-engine/current") != "releases/" + BASELINE):
        raise RuntimeError("live controller changed")


def _trusted_dir(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_gid != 0 or info.st_nlink < 2
            or stat.S_IMODE(info.st_mode) not in (0o700, 0o755)):
        raise RuntimeError("unexpected code directory")


def _trusted_source(source: Path) -> None:
    if source.name != "source" or source.parent.name != os.environ.get("AE_REQUEST_ID"):
        raise RuntimeError("unexpected source")
    for name in ("secure_vault.py", "vault_runtime.py"):
        original = source / "ai_agent_engine" / name
        installed = RELEASE / "ai_agent_engine" / name
        if (original.is_symlink() or installed.is_symlink()
                or not original.is_file() or not installed.is_file()):
            raise RuntimeError("unexpected installed module")
        expected = hashlib.sha256(original.read_bytes()).digest()
        if hashlib.sha256(installed.read_bytes()).digest() != expected:
            raise RuntimeError("installed Vault code does not match source")


def _run(*args: str, timeout: int = 35) -> None:
    result = subprocess.run(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError("service check failed")


def _request(operation: str, **values: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(SOCKET))
        connection.sendall(json.dumps({"operation": operation, **values},
                                      separators=(",", ":")).encode() + b"\n")
        data = bytearray()
        while not data.endswith(b"\n") and len(data) < 4096:
            chunk = connection.recv(1)
            if not chunk:
                break
            data += chunk
    if not data.endswith(b"\n") or len(data) >= 4096:
        raise RuntimeError("Vault health absent")
    response = json.loads(data)
    if type(response) is not dict or response.get("ok") is not True:
        raise RuntimeError("Vault health denied")
    return response


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    _check_controller()
    paths = (VAULT_CODE_ROOT, VAULT_CODE_ROOT / "releases",
             RELEASE, RELEASE / "ai_agent_engine")
    for directory in paths:
        _trusted_dir(directory)
    _trusted_source(Path.cwd())
    # All prechecks pass before the first chmod. Only these code directories.
    _check_controller()
    for directory in paths:
        os.chmod(directory, 0o755)
    _run("/usr/bin/systemctl", "reset-failed", "ae-vault.service")
    _run("/usr/bin/systemctl", "restart", "ae-vault.service", timeout=60)
    for _ in range(20):
        try:
            if _request("status").get("status") == "ready":
                break
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.25)
    else:
        raise RuntimeError("Vault still unavailable")
    _request("ensure_project", project_id="ai-agent-engine")
    _run("/usr/bin/systemctl", "start", "ae-vault-backup.service", timeout=120)
    _run("/usr/bin/systemctl", "enable", "--now", "ae-vault-backup.timer")
    for unit in ("ae-vault.socket", "ae-vault.service", "ae-vault-backup.timer"):
        _run("/usr/bin/systemctl", "is-active", "--quiet", unit)
    copies = list(BACKUP.glob("vault-*.aev"))
    if not copies or not all((file.stat().st_mode & 0o777) == 0o600
                             for file in copies):
        raise RuntimeError("encrypted backup missing")
    _check_controller()
    print("VAULT_REPAIR=" + json.dumps({
        "status": "ACTIVE_LOCAL_ONLY", "vault_source_sha": VAULT_SOURCE,
        "controller_unchanged": BASELINE,
        "code_dirs": "TRAVERSABLE_ROOT_OWNED", "service": "ACTIVE",
        "socket": "ACTIVE", "backup_timer": "ACTIVE",
        "first_backup": "ENCRYPTED_AND_RESTORE_TESTED",
        "off_host_recovery": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        print("VAULT_REPAIR=HOLD_KEEP_EXISTING_DATA_AND_KEYS")
        raise SystemExit(1)
