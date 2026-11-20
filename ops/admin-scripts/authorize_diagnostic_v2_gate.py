"""Authenticated owner-admin switch of ONLY the signed add-on allowlist.

The existing sudo path, release signer, live controller, task databases and
diagnostic unit are not changed here. The old operator is retained under
root-private evidence for byte-exact rollback. This script does NOT run QA.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
ROOT_OPERATOR = Path("/usr/local/libexec/ae-release/diagnostic_addon_operator.py")
OLD_INSTALLER = Path("/usr/local/libexec/ae-release/activate_managed_diagnostics_7_7.py")
NEW_INSTALLER = Path("/usr/local/libexec/ae-release/upgrade_managed_diagnostics_7_7_v2.py")
SIGNERS = Path("/etc/ai-agent-engine/release-signers")
OLD_OPERATOR_SHA = "7d2779ccdf55c464ea31e6e52f340dae0cbdaee23d009298e08cf7a760582207"
OLD_INSTALLER_SHA = "bdd6a89574ef8fdad65375d4c026a3faf92d8d639b447669898d7bdfc7e39e14"
NEW_OPERATOR_SHA = "f46f051646e17e6051dcbb7185cdcc7398304588478a7ad3918f35bc5e070de7"
NEW_INSTALLER_SHA = "89f322996d1611184ebce41aa970aa5ea8fd8beb976c53be56308f0723c05b09"
SIGNERS_SHA = "fdbc9ac1009f339bf11e1f2de894bdd42efc944befddd41c7f676f1bc87299c2"
SOURCE_OPERATOR = Path("infra/deploy/diagnostic_addon_operator_v2.py")
SOURCE_INSTALLER = Path("ops/admin-scripts/upgrade_managed_diagnostics_7_7_v2.py")
OLD_SERVICE = "ae-managed-diagnostics.service"
BASELINE = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular(path: Path, *, sha: str, mode: int) -> bytes:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeError("trusted_root_file_changed")
    body = path.read_bytes()
    if _hash(body) != sha:
        raise RuntimeError("trusted_digest_changed")
    return body


def _exclusive(path: Path, content: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _baseline() -> None:
    if os.readlink("/opt/ai-agent-engine/current") != "releases/" + LIVE_SHA:
        raise RuntimeError("live_controller_changed")
    for service in (*BASELINE, OLD_SERVICE):
        subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", service),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       check=True, timeout=10)


def main() -> None:
    if (os.geteuid() != 0 or os.environ.get("AE_EXPECTED_CURRENT") != LIVE_SHA
            or not os.environ.get("AE_SOURCE_SHA")
            or not os.environ.get("AE_REQUEST_SHA")):
        raise RuntimeError("authenticated_owner_admin_required")
    evidence = Path(os.environ["AE_EVIDENCE"])
    info = evidence.lstat()
    if (evidence.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("unsafe_private_evidence")
    _baseline()
    old_operator = _regular(ROOT_OPERATOR, sha=OLD_OPERATOR_SHA, mode=0o444)
    _regular(OLD_INSTALLER, sha=OLD_INSTALLER_SHA, mode=0o444)
    _regular(SIGNERS, sha=SIGNERS_SHA, mode=0o644)
    new_operator = SOURCE_OPERATOR.read_bytes()
    new_installer = SOURCE_INSTALLER.read_bytes()
    if (_hash(new_operator) != NEW_OPERATOR_SHA
            or _hash(new_installer) != NEW_INSTALLER_SHA
            or NEW_INSTALLER.exists() or NEW_INSTALLER.is_symlink()):
        raise RuntimeError("reviewed_v2_gate_not_exact")
    if ROOT_OPERATOR.parent != NEW_INSTALLER.parent:
        raise RuntimeError("unexpected_root_gate_layout")

    # Re-read the still-running release and both protected source identities
    # immediately before modifying any privileged file.
    _baseline()
    _regular(ROOT_OPERATOR, sha=OLD_OPERATOR_SHA, mode=0o444)
    _exclusive(evidence / "diagnostic_addon_operator_v1.py", old_operator, 0o600)
    temp_operator = ROOT_OPERATOR.with_name(".diagnostic_addon_operator_v2.pending")
    created_installer = False
    replaced = False
    try:
        if temp_operator.exists() or temp_operator.is_symlink():
            raise RuntimeError("unexpected_root_operator_staging")
        _exclusive(NEW_INSTALLER, new_installer, 0o444)
        created_installer = True
        _exclusive(temp_operator, new_operator, 0o444)
        os.replace(temp_operator, ROOT_OPERATOR)
        replaced = True
        _regular(ROOT_OPERATOR, sha=NEW_OPERATOR_SHA, mode=0o444)
        _regular(NEW_INSTALLER, sha=NEW_INSTALLER_SHA, mode=0o444)
        _baseline()
        report = {"status": "SIGNED_DIAGNOSTIC_GATE_V2_READY",
                  "allowed_source": "3ef07c51ec287a24bd54d8fcea7c40a99e69b569",
                  "old_operator_retained": True, "sudo_permissions_unchanged": True,
                  "existing_release_signer_unchanged": True,
                  "live_controller_unchanged": True,
                  "diagnostic_runtime_not_upgraded_yet": True}
        _exclusive(evidence / "diagnostic-v2-gate.json",
                   (json.dumps(report, sort_keys=True) + "\n").encode(), 0o600)
        print("SIGNED_GATE_V2 " + json.dumps(report, sort_keys=True), flush=True)
    except Exception:
        if replaced:
            rollback = ROOT_OPERATOR.with_name(".diagnostic_addon_operator_v1.rollback")
            _exclusive(rollback, old_operator, 0o444)
            os.replace(rollback, ROOT_OPERATOR)
        if temp_operator.exists() and not temp_operator.is_symlink():
            temp_operator.rename(evidence / "rejected-v2-operator.py")
        if created_installer:
            NEW_INSTALLER.rename(evidence / "rejected-v2-installer.py")
        raise


if __name__ == "__main__":
    main()
