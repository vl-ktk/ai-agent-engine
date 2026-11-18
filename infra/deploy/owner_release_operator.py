#!/usr/bin/env python3
"""Root-installed, scoped Netcup release operator.

Never stage owner-supplied executable code solely because ae-deploy supplied a
checksum: a fresh, signed owner intent is mandatory. This file and the
trusted promotion implementation must be installed separately as root-owned
read-only files. Importing this source from GitHub grants no server rights.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Iterator

ROOT = Path("/opt/ai-agent-engine")
INCOMING = Path("/home/ae-deploy/ae-incoming")
PROMOTION = Path("/usr/local/libexec/ae-release/promotion.py")
SIGNERS = Path("/etc/ai-agent-engine/release-signers")
SIGNING_PRINCIPAL = "ai-agent-engine-owner"
SIGNING_NAMESPACE = "ai-agent-engine-release@vl-ktk"
SERVICE = "ae-controller.service"
CONFIRMATION = "OWNER_APPROVED_RELEASE_OPERATION"
SHA = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
WHEEL = re.compile(r"[A-Za-z0-9_.+-]+\.whl\Z")


class ReleaseRefused(RuntimeError):
    pass


def _sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ReleaseRefused("invalid release SHA")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ReleaseRefused("invalid independent wheel SHA-256")
    return value


def _read_regular(path: Path, maximum: int) -> bytes:
    """Read one bounded inode, rejecting symlinks and hardlinks."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size <= 0 or info.st_size > maximum):
            raise ReleaseRefused("invalid incoming file type or size")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) != info.st_size or len(data) > maximum:
            raise ReleaseRefused("incoming file changed during read")
        return data
    finally:
        os.close(fd)


def _unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ReleaseRefused("duplicate signed intent key")
        obj[key] = value
    return obj


def _parse_intent(raw: bytes, *, operation: str, target: str, previous: str,
                  digest: str, now: int) -> None:
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (UnicodeError, ValueError) as exc:
        raise ReleaseRefused("invalid signed intent JSON") from exc
    expected = {"schema": 1, "operation": operation, "source_sha": target,
                "expected_current": previous, "wheel_sha256": digest}
    if (not isinstance(data, dict)
            or set(data) != set(expected) | {"issued_at", "expires_at"}
            or any(type(data.get(key)) is not type(value)
                   or data.get(key) != value for key, value in expected.items())):
        raise ReleaseRefused("signed intent does not match the requested operation")
    issued, expires = data["issued_at"], data["expires_at"]
    if (type(issued) is not int or type(expires) is not int
            or issued > now + 120 or issued < now - 1200
            or expires <= now or expires > issued + 1200):
        raise ReleaseRefused("signed owner intent expired or out of bounds")


def _trusted_signers_file(path: Path = SIGNERS, *, owner_uid: int = 0) -> Path:
    info = path.lstat()
    directory = path.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
            or info.st_mode & 0o022 or not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != owner_uid or directory.st_mode & 0o022):
        raise ReleaseRefused("release signing authority is not root controlled")
    return path


def verify_owner_intent(*, operation: str, target: str, previous: str,
                        digest: str, incoming: Path, root: Path,
                        signers: Path = SIGNERS, owner_uid: int = 0,
                        now: int | None = None) -> None:
    """Verify signed, short-lived exact release authorization before any write."""
    source = incoming / target
    if source.is_symlink() or not source.is_dir():
        raise ReleaseRefused("release incoming directory missing")
    raw = _read_regular(source / "release-intent.json", 4096)
    signature = _read_regular(source / "release-intent.json.sig", 4096)
    _parse_intent(raw, operation=operation, target=target, previous=previous,
                  digest=digest, now=int(time.time()) if now is None else now)
    _trusted_signers_file(signers, owner_uid=owner_uid)
    directory = root / "releases"
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseRefused("trusted releases directory missing")
    with tempfile.TemporaryDirectory(prefix=".signature-", dir=directory) as tmp:
        sig = Path(tmp) / "authorization.sig"
        sig.write_bytes(signature)
        try:
            subprocess.run(
                ["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(signers),
                 "-I", SIGNING_PRINCIPAL, "-n", SIGNING_NAMESPACE,
                 "-s", str(sig)],
                input=raw, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=True, timeout=15,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ReleaseRefused("invalid signed release authorization") from exc


@contextmanager
def trusted_bundle(*, incoming: Path, target: str, digest: str,
                   root: Path) -> Iterator[Path]:
    """Copy a potentially mutable ae-deploy bundle to a root-only snapshot.

    No untrusted wheel is executed before its bytes match the signed digest.
    """
    source = incoming / target
    if source.is_symlink() or not source.is_dir():
        raise ReleaseRefused("incoming release directory missing")
    manifest = _read_regular(source / "release-manifest.json", 8192)
    try:
        data = json.loads(manifest)
    except (ValueError, UnicodeError) as exc:
        raise ReleaseRefused("unreadable release manifest") from exc
    name = data.get("wheel_file") if isinstance(data, dict) else None
    if not isinstance(name, str) or not WHEEL.fullmatch(name):
        raise ReleaseRefused("invalid release wheel filename")
    directory = root / "releases"
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseRefused("trusted releases directory missing")
    # Snapshot wheel bytes from a pinned inode. No user-writable path is used
    # for pip install or the controller import smoke in stage_release.
    wheel_bytes = _read_regular(source / name, 100_000_000)
    if hashlib.sha256(wheel_bytes).hexdigest() != digest:
        raise ReleaseRefused("signed release digest mismatch")
    with tempfile.TemporaryDirectory(prefix=".verified-bundle-", dir=directory) as tmp:
        safe = Path(tmp)
        (safe / "release-manifest.json").write_bytes(manifest)
        (safe / name).write_bytes(wheel_bytes)
        yield safe


def load_trusted_promotion():
    """Load only the root-owned installed implementation, never the checkout."""
    if PROMOTION.is_symlink():
        raise ReleaseRefused("promotion implementation must not be a symlink")
    info = PROMOTION.stat()
    if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ReleaseRefused("promotion implementation is not root controlled")
    spec = importlib.util.spec_from_file_location("ae_trusted_promotion", PROMOTION)
    if spec is None or spec.loader is None:
        raise ReleaseRefused("promotion implementation unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restart_and_check() -> None:
    subprocess.run(["/usr/bin/systemctl", "restart", SERVICE], check=True, timeout=60)
    subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", SERVICE],
                   check=True, timeout=15)
    python = ROOT / "current" / "venv" / "bin" / "python"
    if not python.is_file():
        raise ReleaseRefused("current release Python is missing")
    subprocess.run(
        ["/usr/sbin/runuser", "-u", "ae-runtime", "--", str(python), "-I",
         "-m", "ai_agent_engine.controller", "check", "--max-age", "15"],
        check=True, timeout=30,
    )


def perform(
    promotion, *, operation: str, source_sha: str, expected_current: str,
    wheel_sha256: str, restart=restart_and_check,
    root: Path = ROOT, incoming: Path = INCOMING,
    authorize=verify_owner_intent, bundle_factory=trusted_bundle,
) -> None:
    """Owner-authorized CAS activation; failure attempts one safe rollback."""
    target = _sha(source_sha)
    previous = _sha(expected_current)
    digest = _digest(wheel_sha256)
    if operation not in ("activate", "rollback") or target == previous:
        raise ReleaseRefused("unsupported operation or unchanged release")
    if promotion.active_sha(root) != previous:
        raise ReleaseRefused("active release changed; refusing operation")
    # A valid SSH login and knowledge of a wheel hash ARE NOT authorization.
    authorize(operation=operation, target=target, previous=previous,
              digest=digest, incoming=incoming, root=root)
    if operation == "activate":
        if (root / "releases" / target).exists():
            raise ReleaseRefused("target already staged; use rollback operation")
        with bundle_factory(incoming=incoming, target=target, digest=digest,
                            root=root) as bundle:
            promotion.stage_release(
                bundle, root, target, expected_wheel_sha256=digest,
                interpreter="/usr/bin/python3",
            )
    else:
        staged = root / "releases" / target
        if staged.is_symlink() or not staged.is_dir():
            raise ReleaseRefused("rollback target must be an existing immutable release")
    promotion.promote_release(root, target, expected_current=previous)
    try:
        restart()
    except Exception as failure:
        try:
            promotion.promote_release(root, previous, expected_current=target)
            restart()
        except Exception as rollback_error:
            raise ReleaseRefused(
                "activation failed and automatic rollback also failed; manual recovery required"
            ) from rollback_error
        raise ReleaseRefused("activation failed; previous release restored") from failure


def main() -> int:
    parser = argparse.ArgumentParser(description="Restricted root-owned Netcup release operator")
    parser.add_argument("--operation", choices=["activate", "rollback"], required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--expected-current", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if args.confirmation != CONFIRMATION:
        parser.error("missing workflow confirmation; signed intent is also required")
    if os.geteuid() != 0:
        parser.error("operator must be executed as root via the restricted sudoers rule")
    try:
        perform(load_trusted_promotion(), operation=args.operation,
                source_sha=args.source_sha, expected_current=args.expected_current,
                wheel_sha256=args.wheel_sha256)
    except (ReleaseRefused, ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print("PASS: signed verified release activated and controller healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
