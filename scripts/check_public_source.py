"""Conservative offline guard against publishing obvious secrets in Git.

Scans tracked files only (including force-added ignored files). Never prints
matched content. This is defense in depth, NOT proof that a repo is secret-free.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


MAX_PUBLIC_FILE_BYTES = 2_000_000

_CREDENTIAL_NAMES = {
    "session.json", "host.json", "pending-registration.json",
    "id_rsa", "id_ed25519", "credentials.json", "service-account.json",
}
_CREDENTIAL_DIRECTORIES = {"secrets", "credentials", "private-keys", ".ssh"}
_CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3")

_SIGNATURES = (
    ("private_key_block", re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("github_token", re.compile(
        rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("openai_api_key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{24,}\b")),
)


def tracked_files(root: Path) -> tuple[str, ...]:
    """Use the Git index, so gitignored files force-added to Git are included."""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return tuple(os.fsdecode(name) for name in result.stdout.split(b"\x00") if name)


def scan_files(root: Path, paths: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Return only safe (path, rule-id) findings; never matched bytes/text."""
    violations: list[tuple[str, str]] = []
    real_root = root.resolve(strict=True)
    for name in paths:
        if (not isinstance(name, str) or not name
                or "\\" in name or "\x00" in name
                or PurePosixPath(name).is_absolute()
                or any(part in ("", ".", "..") for part in name.split("/"))):
            violations.append((str(name), "invalid_tracked_path"))
            continue
        relative = PurePosixPath(name)
        lowered = tuple(part.casefold() for part in relative.parts)
        basename = lowered[-1]
        if (basename in _CREDENTIAL_NAMES
                or basename == ".env" or basename.startswith(".env.")
                or basename.endswith(_CREDENTIAL_SUFFIXES)
                or any(part in _CREDENTIAL_DIRECTORIES for part in lowered[:-1])):
            violations.append((name, "credential_file_path"))
            continue
        file_path = root.joinpath(*relative.parts)
        if (file_path.is_symlink() or not file_path.is_file()
                or not file_path.resolve(strict=True).is_relative_to(real_root)):
            violations.append((name, "unsafe_tracked_file"))
            continue
        try:
            if file_path.stat().st_size > MAX_PUBLIC_FILE_BYTES:
                violations.append((name, "oversized_public_file"))
                continue
            raw = file_path.read_bytes()
            if len(raw) > MAX_PUBLIC_FILE_BYTES:
                violations.append((name, "oversized_public_file"))
                continue
            raw.decode("utf-8")
        except (OSError, UnicodeError):
            violations.append((name, "unreadable_or_binary_file"))
            continue
        if b"\x00" in raw:
            violations.append((name, "unreadable_or_binary_file"))
            continue
        for rule, signature in _SIGNATURES:
            if signature.search(raw):
                violations.append((name, rule))
    return tuple(violations)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        violations = scan_files(root, tracked_files(root))
    except (OSError, subprocess.CalledProcessError):
        print("Public-source scan could not inspect the Git index", file=sys.stderr)
        return 2
    if violations:
        for name, rule in violations:
            print("BLOCKED public file: " + repr(name) + " (" + rule + ")", file=sys.stderr)
        return 1
    print("Public-source scan: PASS (tracked paths and high-confidence signatures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
