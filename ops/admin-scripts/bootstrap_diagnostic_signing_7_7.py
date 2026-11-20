"""One-time owner-admin bootstrap of public signing trust for diagnostic 7.7.

No service activation, SSH/sudoers changes, controller release promotion,
secret creation or database writes. Only three previously absent root files.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
PUBLIC_SIGNER = (
    "ai-agent-engine-owner ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIBY32VNMOtjzt2eaC81HWbyaHcudJyzBBYtN4yFk+P+C\n"
)
SIGNERS = Path("/etc/ai-agent-engine/release-signers")
OPERATOR = Path("/usr/local/libexec/ae-release/diagnostic_addon_operator.py")
INSTALLER = Path("/usr/local/libexec/ae-release/activate_managed_diagnostics_7_7.py")
CURRENT = Path("/opt/ai-agent-engine/current")
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")
SOURCE_OPERATOR = Path("infra/deploy/diagnostic_addon_operator.py")
SOURCE_INSTALLER = Path("ops/admin-scripts/activate_managed_diagnostics_7_7.py")
OPERATOR_SHA256 = "7d2779ccdf55c464ea31e6e52f340dae0cbdaee23d009298e08cf7a760582207"
INSTALLER_SHA256 = "bdd6a89574ef8fdad65375d4c026a3faf92d8d639b447669898d7bdfc7e39e14"


def _root_parent(path: Path, *, create: bool) -> None:
    directory = Path("/")
    for part in path.parent.parts[1:]:
        directory = directory / part
        if not directory.exists() and create:
            directory.mkdir(mode=0o755)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise RuntimeError("unsafe_privileged_parent")


def _baseline(expected: str) -> None:
    if (expected != LIVE_SHA or not CURRENT.is_symlink()
            or os.readlink(CURRENT) != "releases/" + LIVE_SHA):
        raise RuntimeError("live_release_changed")
    for unit in UNITS:
        subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", unit),
                       stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=10, check=True)


def _source_bytes(path: Path, sha: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("untrusted_bootstrap_source")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != sha:
        raise RuntimeError("reviewed_bootstrap_source_changed")
    return content


def _exclusive_root_file(path: Path, content: bytes, mode: int, *,
                         created: list[Path] | None = None,
                         expected_uid: int = 0) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    if created is not None:
        # Track the new inode immediately, so even a mid-write failure is
        # moved into the private recovery directory by bootstrap().
        created.append(path)
    try:
        with os.fdopen(os.dup(fd), "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # netcup_admin_remote.py correctly applies umask(077). Its masked
        # creation modes are not the reviewed final modes; set them on the
        # newly opened inode, without opening or following the path again.
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    info = path.lstat()
    if (info.st_uid != expected_uid or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeError("privileged_file_permissions_wrong")
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(content).digest():
        raise RuntimeError("privileged_file_contents_wrong")


def _check_evidence(evidence: Path) -> None:
    if (evidence.is_symlink() or not evidence.is_dir()
            or evidence.stat().st_uid != 0 or stat.S_IMODE(evidence.stat().st_mode) != 0o700):
        raise RuntimeError("owner_admin_evidence_missing")


def bootstrap(*, expected: str, evidence: Path) -> dict:
    _baseline(expected)
    _check_evidence(evidence)
    operator = _source_bytes(SOURCE_OPERATOR, OPERATOR_SHA256)
    installer = _source_bytes(SOURCE_INSTALLER, INSTALLER_SHA256)
    targets = [(SIGNERS, PUBLIC_SIGNER.encode(), 0o644),
               (OPERATOR, operator, 0o444), (INSTALLER, installer, 0o444)]
    # No overwrite, including a path that is a dangling symlink.
    if any(path.exists() or path.is_symlink() for path, _, _ in targets):
        raise RuntimeError("release_signing_target_already_present")
    for path, _, _ in targets:
        _root_parent(path, create=False) if path.parent.exists() else None
    _baseline(expected)
    created = []
    try:
        for path, data, mode in targets:
            _root_parent(path, create=True)
            _exclusive_root_file(path, data, mode, created=created)
        _baseline(expected)
        report = {"status": "DIAGNOSTIC_SIGNING_BOOTSTRAPPED",
                  "scope": "vl-ktk/ai-agent-engine", "live_release": LIVE_SHA,
                  "signer_public_only": True, "trusted_installer_pinned": True,
                  "restricted_addon_operator_installed": True,
                  "new_sudo_or_ssh_permissions": False,
                  "core_release_changed": False, "diagnostic_service_started": False}
        path = evidence / "diagnostic-signing-bootstrap-7-7.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, sort_keys=True)
            output.write("\n")
        return report
    except Exception:
        for path in reversed(created):
            if path.is_file() and not path.is_symlink():
                shutil.move(str(path), str(evidence / path.name))
        raise


def main() -> None:
    if os.geteuid() != 0 or not all(os.environ.get(name) for name in
                                   ("AE_EXPECTED_CURRENT", "AE_SOURCE_SHA",
                                    "AE_REQUEST_SHA", "AE_EVIDENCE")):
        raise RuntimeError("authenticated_owner_admin_required")
    result = bootstrap(expected=os.environ["AE_EXPECTED_CURRENT"],
                       evidence=Path(os.environ["AE_EVIDENCE"]))
    print("DIAGNOSTIC_SIGNING_7_7 " + json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
