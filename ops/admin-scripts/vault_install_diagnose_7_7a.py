#!/usr/bin/env python3
"""Read-only metadata after a failed Vault install; never reads secret values."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import grp
import stat
import subprocess


BASELINE = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
VAULT_SOURCE = "38d0e960d63f725ee5d5de5fbf71c8e03052ce52"
UNIT_NAMES = ("ae-vault.socket", "ae-vault.service", "ae-vault-backup.service",
              "ae-vault-backup.timer")


def _metadata(path: Path) -> dict[str, object]:
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return {"present": False}
    return {"present": True,
            "type": "directory" if stat.S_ISDIR(meta.st_mode) else
                    "file" if stat.S_ISREG(meta.st_mode) else
                    "other",
            "private": not bool(stat.S_IMODE(meta.st_mode) & 0o077),
            "uid_root": meta.st_uid == 0}


def _unit(name: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", name, "--no-pager",
             "--property=ActiveState,SubState,Result,ExecMainStatus,LoadState"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "command_unavailable"}
    allowed = {"ActiveState", "SubState", "Result", "ExecMainStatus", "LoadState"}
    known_values = {"active", "inactive", "failed", "activating", "deactivating",
                    "loaded", "not-found", "success", "exit-code", "timeout",
                    "signal", "dead", "running", "listening", "waiting", "start", "stop"}
    output: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in allowed:
            continue
        output[key] = value if value in known_values or value.isdecimal() else "other"
    return output


def main() -> int:
    if (os.geteuid() != 0 or os.getenv("AE_EXPECTED_CURRENT") != BASELINE
            or os.readlink("/opt/ai-agent-engine/current") != "releases/" + BASELINE):
        print("VAULT_DIAG=HOLD_BASELINE")
        return 1
    release = Path("/opt/ai-agent-engine-vault/releases") / VAULT_SOURCE
    target = {
        "status": "METADATA_ONLY",
        "controller_unchanged": BASELINE,
        "ae_vault_user": "present" if "ae-vault" in {entry.pw_name for entry in pwd.getpwall()} else "missing",
        "ae_runtime_primary_group": grp.getgrgid(pwd.getpwnam("ae-runtime").pw_gid).gr_name,
        "targets": {
            "root": _metadata(Path("/var/lib/ai-agent-engine-vault")),
            "release_dir": _metadata(release),
            "credential_dir": _metadata(Path("/etc/ai-agent-engine/vault")),
            "master_credential": _metadata(Path("/etc/ai-agent-engine/vault/master.cred")),
            "backup_credential": _metadata(Path("/etc/ai-agent-engine/vault/backup.cred")),
            "backup_dir": _metadata(Path("/var/backups/ai-agent-engine-vault")),
            "socket": _metadata(Path("/run/ae-vault.sock")),
        },
        "units": {name: _unit(name) for name in UNIT_NAMES},
    }
    print("VAULT_DIAG=" + json.dumps(target, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError):
        print("VAULT_DIAG=UNAVAILABLE")
        raise SystemExit(1)
