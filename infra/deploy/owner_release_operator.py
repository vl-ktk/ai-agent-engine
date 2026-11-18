#!/usr/bin/env python3
"""Root-installed, strictly scoped Netcup release activation helper.

This source file is not a grant of root rights. It must be installed
independently as root-owned immutable code beside a separately reviewed
promotion.py; GitHub workflows cannot install it via ae-deploy.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess


ROOT = Path("/opt/ai-agent-engine")
INCOMING = Path("/home/ae-deploy/ae-incoming")
PROMOTION = Path("/usr/local/libexec/ae-release/promotion.py")
SERVICE = "ae-controller.service"
CONFIRMATION = "OWNER_APPROVED_RELEASE_OPERATION"
SHA = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


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


def load_trusted_promotion():
    """Load only the root-owned installed implementation, never the checkout."""
    if PROMOTION.is_symlink():
        raise ReleaseRefused("promotion implementation must not be a symlink")
    info = PROMOTION.stat()
    if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ReleaseRefused("promotion implementation is not root-controlled")
    spec = importlib.util.spec_from_file_location("ae_trusted_promotion", PROMOTION)
    if spec is None or spec.loader is None:
        raise ReleaseRefused("promotion implementation unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restart_and_check() -> None:
    """Only the one approved service can be restarted by this helper."""
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
) -> None:
    """CAS activation/rollback; failure attempts to restore old release."""
    target = _sha(source_sha)
    previous = _sha(expected_current)
    if operation not in ("activate", "rollback") or target == previous:
        raise ReleaseRefused("unsupported operation or unchanged release")
    if promotion.active_sha(root) != previous:
        raise ReleaseRefused("active release changed; refusing operation")
    if operation == "activate":
        digest = _digest(wheel_sha256)
        bundle = incoming / target
        if bundle.is_symlink() or not bundle.is_dir():
            raise ReleaseRefused("incoming verified release directory missing")
        if (root / "releases" / target).exists():
            raise ReleaseRefused("target is already staged; use rollback operation")
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
        parser.error("explicit owner workflow confirmation required")
    if os.geteuid() != 0:
        parser.error("operator must be executed as root via the restricted sudoers rule")
    try:
        perform(load_trusted_promotion(), operation=args.operation,
                source_sha=args.source_sha,
                expected_current=args.expected_current,
                wheel_sha256=args.wheel_sha256)
    except (ReleaseRefused, ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print("PASS: verified release operation and controller health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
