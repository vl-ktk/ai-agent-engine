"""Offline-verified, owner-operated immutable release staging and promotion.

This program is NEVER invoked by automatic CI or a VPS service. It does not
invoke sudo/systemctl, access SSH, use API credentials or restart anything.
Promoting the current symlink on the production host requires the owner's
separate explicit authorization and a privileged operator.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Iterator


SHA = re.compile(r"[0-9a-f]{40}\Z")
WHEEL = re.compile(r"[A-Za-z0-9_.+-]+\.whl\Z")
CONFIRM = "OWNER_APPROVED_RELEASE_OPERATION"


class PromotionError(ValueError):
    """Refuse a release instead of guessing its identity or modifying paths."""


def require_sha(value: str) -> str:
    if not SHA.fullmatch(value):
        raise PromotionError("Release must use an exact lowercase 40-character Git SHA")
    return value


def _root(directory: Path) -> Path:
    # Refuse arbitrary broad roots. Unit paths depend on /ai-agent-engine.
    if (not directory.is_absolute() or directory.name != "ai-agent-engine"
            or directory.is_symlink() or directory.resolve() != directory
            or not directory.is_dir()
            or directory == Path("/")):
        raise PromotionError("Use an existing absolute non-symlink ai-agent-engine root")
    return directory


@contextmanager
def _promotion_lock(root: Path) -> Iterator[None]:
    """Serialize owner-operated promotion; lock survives abrupt exit safely."""
    import fcntl

    descriptor = os.open(root / ".promotion.lock", os.O_CREAT | os.O_RDWR |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PromotionError("Another release promotion is running") from exc
        yield
    finally:
        os.close(descriptor)


def verified_bundle(
    bundle: Path, selected_sha: str, *, expected_wheel_sha256: str | None = None,
) -> tuple[Path, dict]:
    """Validate prepared-only metadata and exact wheel bytes before install."""
    require_sha(selected_sha)
    if not bundle.is_absolute() or bundle.is_symlink() or not bundle.is_dir():
        raise PromotionError("Release bundle must be an absolute real directory")
    manifest_path = bundle / "release-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 8192:
        raise PromotionError("Invalid release manifest")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise PromotionError("Unreadable release manifest") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise PromotionError("Unsupported release manifest")
    if (data.get("mode") != "prepared_only_no_vps_deploy"
            or data.get("controller_service_activated") is not False
            or data.get("source_commit") != selected_sha):
        raise PromotionError("Manifest is not an approved prepared-only source")
    if not SHA.fullmatch(str(data.get("validated_main_head", ""))):
        raise PromotionError("Manifest has no validated main SHA")
    name = data.get("wheel_file")
    if not isinstance(name, str) or not WHEEL.fullmatch(name):
        raise PromotionError("Manifest contains an invalid wheel name")
    wheel = bundle / name
    if wheel.is_symlink() or not wheel.is_file():
        raise PromotionError("Prepared wheel missing or symlinked")
    size = wheel.stat().st_size
    if size <= 0 or size > 100_000_000 or type(data.get("wheel_bytes")) is not int or data["wheel_bytes"] != size:
        raise PromotionError("Prepared wheel size mismatch")
    expected = data.get("wheel_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise PromotionError("Invalid prepared wheel digest")
    with wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected:
        raise PromotionError("Prepared wheel SHA-256 mismatch")
    if expected_wheel_sha256 is not None and digest != expected_wheel_sha256:
        raise PromotionError("Independent owner-reviewed wheel SHA-256 mismatch")
    return wheel, data


def stage_release(
    bundle: Path, root: Path, sha: str, *, expected_wheel_sha256: str,
    interpreter: str = sys.executable,
) -> Path:
    """Install wheel into a brand new immutable release; never switch current."""
    root = _root(root)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_wheel_sha256):
        raise PromotionError("Independent SHA-256 is mandatory for staging")
    wheel, data = verified_bundle(bundle, sha, expected_wheel_sha256=expected_wheel_sha256)
    releases = root / "releases"
    if releases.is_symlink() or not releases.is_dir():
        raise PromotionError("Release directory must exist and not be a symlink")
    target = releases / sha
    if target.exists() or target.is_symlink():
        raise PromotionError("Release already exists; refusing overwrite")
    with TemporaryDirectory(prefix=".staging-", dir=releases) as temporary:
        staging = Path(temporary)
        subprocess.run([interpreter, "-m", "venv", str(staging / "venv")], check=True)
        executable = staging / "venv" / "bin" / "python"
        if not executable.is_file():
            raise PromotionError("Expected POSIX isolated Python runtime not found")
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        subprocess.run([str(executable), "-I", "-m", "pip", "install", "--no-index",
                        "--no-deps", str(wheel)], check=True, env=environment)
        subprocess.run([str(executable), "-I", "-c",
                        "from ai_agent_engine.controller import check_health, main; "
                        "assert main(['check', '--health-file', '/nonexistent/ae-health']) == 1"],
                       check=True, env=environment, cwd=staging)
        (staging / "release-manifest.json").write_text(
            json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8",
        )
        # TemporaryDirectory defaults to 0700; the non-root service must read
        # root-owned release code, while never being able to modify it.
        os.chmod(staging, 0o755)
        # Atomic within the same filesystem; no current symlink or service touched.
        if target.exists() or target.is_symlink():
            raise PromotionError("Release appeared concurrently; refusing overwrite")
        os.rename(staging, target)
    return target


def active_sha(root: Path) -> str | None:
    root = _root(root)
    current = root / "current"
    if not current.is_symlink():
        if current.exists():
            raise PromotionError("Current is not a symlink; refusing changes")
        return None
    target = os.readlink(current)
    if not re.fullmatch(r"releases/[0-9a-f]{40}", target):
        raise PromotionError("Current release points outside managed release directory")
    sha = target.removeprefix("releases/")
    if (root / target).is_symlink() or not (root / target).is_dir():
        raise PromotionError("Current release target does not exist")
    return sha


def promote_release(root: Path, sha: str, *, expected_current: str | None) -> str | None:
    """Atomic link switch with compare-and-swap; caller handles service restart."""
    require_sha(sha)
    if expected_current is not None:
        require_sha(expected_current)
    root = _root(root)
    if (root / "releases").is_symlink():
        raise PromotionError("Release directory must not be symlinked")
    target = root / "releases" / sha
    if target.is_symlink() or not (target / "venv" / "bin" / "python").is_file():
        raise PromotionError("Staged immutable release missing")
    with _promotion_lock(root):
        previous = active_sha(root)
        if previous != expected_current:
            raise PromotionError("Current release changed; refusing promotion")
        next_link = root / f".current-next-{os.getpid()}"
        if next_link.exists() or next_link.is_symlink():
            raise PromotionError("Temporary link already exists")
        try:
            next_link.symlink_to(f"releases/{sha}")
            # One filesystem atomic switch. No running service is restarted here.
            if active_sha(root) != expected_current:
                raise PromotionError("Current release changed during promotion")
            os.replace(next_link, root / "current")
        finally:
            next_link.unlink(missing_ok=True)
        return previous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "stage", "promote"):
        part = commands.add_parser(name)
        part.add_argument("--source-sha", required=True)
        if name != "promote":
            part.add_argument("--bundle", type=Path, required=True)
            part.add_argument("--expected-wheel-sha256", required=True,
                              help="Digest independently confirmed from the owner-reviewed artifact")
        if name != "verify":
            part.add_argument("--root", type=Path, required=True)
            part.add_argument("--confirmation", required=True)
        if name == "promote":
            part.add_argument("--expected-current", required=True,
                              help="exact prior Git SHA, or NONE for first promotion")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verified_bundle(args.bundle, args.source_sha,
                            expected_wheel_sha256=args.expected_wheel_sha256)
            print("PASS: prepared bundle verified; no host changes")
            return 0
        if args.confirmation != CONFIRM:
            raise PromotionError("Missing explicit owner-operation confirmation")
        if os.name != "posix":
            raise PromotionError("Deployment helper requires POSIX Python")
        if args.command == "stage":
            stage_release(args.bundle, args.root, args.source_sha,
                          expected_wheel_sha256=args.expected_wheel_sha256)
            print("PASS: immutable release staged; current and service unchanged")
        else:
            previous = None if args.expected_current == "NONE" else require_sha(args.expected_current)
            result = promote_release(args.root, args.source_sha, expected_current=previous)
            print(f"PASS: release link switched; prior={result or 'NONE'}; service NOT restarted")
    except (PromotionError, OSError, subprocess.CalledProcessError) as exc:
        print(f"BLOCKED: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
