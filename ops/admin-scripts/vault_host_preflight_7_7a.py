#!/usr/bin/env python3
"""7.7A metadata-only Netcup preflight; DOES NOT install or read secrets.

Executed only through the existing pinned owner-admin workflow. The workflow
itself writes immutable execution evidence under /root/ae-admin-jobs; this
script creates no Vault files and never prints private file contents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


EXPECTED_CONTROLLER = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
CONTROLLER_LINK = Path("/opt/ai-agent-engine/current")
VAULT_ROOT = Path("/var/lib/ai-agent-engine-vault")
VAULT_KEY = Path("/etc/ai-agent-engine/vault/credential.encrypted")
UNIT_PATH = Path("/etc/systemd/system/ae-vault.service")


def _fixed_read(args: list[str]) -> tuple[int, str]:
    """Run fixed local metadata tools, no shell, sanitize all output."""
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=10, check=False)
        return result.returncode, result.stdout.strip()[:256]
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def _safe_metadata(path: Path) -> dict[str, object]:
    """Report only existence/type/mode, never contents, names or owner UID."""
    try:
        meta = path.lstat()
        kind = ("dir" if stat.S_ISDIR(meta.st_mode) else
                "file" if stat.S_ISREG(meta.st_mode) else "other")
        return {"exists": True, "type": kind,
                "private": (stat.S_IMODE(meta.st_mode) & 0o077) == 0,
                "root_owned": meta.st_uid == 0}
    except FileNotFoundError:
        return {"exists": False}


def main() -> int:
    if os.geteuid() != 0:
        print("VAULT_PREFLIGHT.status=DENIED_NOT_ROOT")
        return 1
    expected = os.getenv("AE_EXPECTED_CURRENT")
    if expected != EXPECTED_CONTROLLER:
        print("VAULT_PREFLIGHT.status=HOLD_BASELINE")
        return 1
    try:
        live = os.readlink(CONTROLLER_LINK)
    except OSError:
        print("VAULT_PREFLIGHT.status=HOLD_CONTROLLER_UNKNOWN")
        return 1
    if live != "releases/" + expected:
        print("VAULT_PREFLIGHT.status=HOLD_CONTROLLER_CHANGED")
        return 1

    systemd_bin = shutil.which("systemd-creds")
    has_credential_tool = bool(systemd_bin)
    version_code, version_output = _fixed_read(["/usr/bin/systemctl", "--version"])
    # Report version number only; not environment, file paths or credentials.
    version = version_output.splitlines()[0].split()[:2]
    version = version if version_code == 0 else []
    free = shutil.disk_usage("/var/lib").free
    identity_code, identity_text = _fixed_read(["/usr/bin/id", "-u", "ae-runtime"])

    result = {
        "status": "READ_ONLY_PASS",
        "live_controller_sha": expected,
        "systemd": " ".join(version),
        "systemd_creds_available": has_credential_tool,
        "ae_runtime_user_present": identity_code == 0 and identity_text.isdigit(),
        "free_mib_floor": (free // 1048576 // 100) * 100,
        "vault_root": _safe_metadata(VAULT_ROOT),
        "vault_credential": _safe_metadata(VAULT_KEY),
        "vault_unit": _safe_metadata(UNIT_PATH),
    }
    # Fixed short metadata JSON only; no key reads or external requests.
    print("VAULT_PREFLIGHT=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
