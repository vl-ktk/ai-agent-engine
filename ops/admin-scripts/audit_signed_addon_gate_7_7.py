"""Read-only owner-gated audit before choosing a signed 7.7 add-on deployment.

Never reads secret material, authorizes a release, creates service/socket,
modifies credentials or changes any existing application data. The existing
owner-admin transport creates a private evidence record independently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import stat
import subprocess


CURRENT = Path("/opt/ai-agent-engine/current")
OLD_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
SIGNERS = Path("/etc/ai-agent-engine/release-signers")
OPERATOR = Path("/usr/local/libexec/ae-release/owner_release_operator.py")
PROMOTION = Path("/usr/local/libexec/ae-release/promotion.py")
DIAGNOSTIC = Path("/var/lib/ai-agent-engine-diagnostics")
BACKUPS = Path("/var/backups/ai-agent-engine-diagnostics")
NEW_UNIT = Path("/etc/systemd/system/ae-managed-diagnostics.service")
NEW_CODE = Path("/opt/ai-agent-engine/diagnostic-7-7")
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def _marker(path: Path, owner: int, *, directory: bool = False) -> str:
    """Return fixed bounded status, never file contents or key material."""
    try:
        mode = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    if (stat.S_ISLNK(mode.st_mode) or
            not (stat.S_ISDIR(mode.st_mode) if directory else stat.S_ISREG(mode.st_mode))):
        return "unsafe_type"
    if mode.st_uid != owner or mode.st_mode & 0o022:
        return "unsafe_owner_or_mode"
    return "present_private"


def _active(unit: str) -> bool:
    return subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", unit),
                          stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=8, check=False).returncode == 0


def inventory(expected: str, *, uid: int) -> dict:
    if expected != OLD_SHA or os.readlink(CURRENT) != "releases/" + expected:
        raise RuntimeError("current release changed")
    units = {unit: _active(unit) for unit in UNITS}
    if not all(units.values()):
        raise RuntimeError("baseline unit stopped")
    paths = {
        "root_release_signers": _marker(SIGNERS, 0),
        "root_release_operator": _marker(OPERATOR, 0),
        "root_promotion_helper": _marker(PROMOTION, 0),
        "approved_private_project": _marker(DIAGNOSTIC, uid, directory=True),
        "diagnostic_backup_root": _marker(BACKUPS, uid, directory=True),
        "managed_unit_target": _marker(NEW_UNIT, 0),
        "managed_code_target": _marker(NEW_CODE, 0, directory=True),
    }
    if paths["managed_unit_target"] != "missing" or paths["managed_code_target"] != "missing":
        raise RuntimeError("new managed service target no longer empty")
    # A present signer is only a *public* trust-root marker; it does not
    # establish that GitHub has the matching private key or that a compatible
    # add-on-specific signed command already exists.
    signer_ready = all(paths[key] == "present_private" for key in (
        "root_release_signers", "root_release_operator", "root_promotion_helper"))
    return {
        "status": "SIGNED_ADDON_GATE_INVENTORIED", "scope": "vl-ktk/ai-agent-engine",
        "live_release": expected, "base_units_active": True,
        "paths": paths, "signer_files_present": signer_ready,
        "addon_signed_deployment_exists": False,
        "service_started": False, "new_permissions_or_secrets_created": False,
        "read_only_audit": True,
    }


def main() -> None:
    if os.geteuid() != 0 or not all(os.environ.get(k) for k in
                                   ("AE_REQUEST_SHA", "AE_SOURCE_SHA", "AE_EVIDENCE", "AE_EXPECTED_CURRENT")):
        raise RuntimeError("owner authenticated host audit required")
    report = inventory(os.environ["AE_EXPECTED_CURRENT"],
                       uid=pwd.getpwnam("ae-runtime").pw_uid)
    evidence = Path(os.environ["AE_EVIDENCE"])
    if _marker(evidence, 0, directory=True) != "present_private":
        raise RuntimeError("missing private audit evidence")
    destination = evidence / "signed-addon-inventory-7-7.json"
    with destination.open("x", encoding="utf-8") as output:
        json.dump(report, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
    print("SIGNED_ADDON_GATE_7_7 " + json.dumps(report, separators=(",", ":"),
                                               sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
