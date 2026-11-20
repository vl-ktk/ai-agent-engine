#!/usr/bin/env python3
"""Fixed, root-installed signed installer for the isolated 7.7 diagnostic add-on.

This program never promotes the controller's current release. Its only accepted
artifact is the previously reviewed 7.7 commit, installed under a separate
root-owned code tree by the separately pinned host installer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import zipfile


LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
SOURCE_SHA = "60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23"
RUNTIME_SHA256 = "dcf077701d2fd92fe6cef5cebcd8966e0fd3217c7c8c5a88eb3de447ec4e769b"
OPERATION = "activate-managed-diagnostics-7-7"
CONFIRMATION = "ACTIVATE_SIGNED_DIAGNOSTICS_7_7"
SIGNERS = Path("/etc/ai-agent-engine/release-signers")
INSTALLER = Path("/usr/local/libexec/ae-release/activate_managed_diagnostics_7_7.py")
INCOMING = Path("/home/ae-deploy/ae-incoming")
RELEASES = Path("/opt/ai-agent-engine/releases")
CURRENT = Path("/opt/ai-agent-engine/current")
EVIDENCE = Path("/root/ae-diagnostic-release-evidence")
SIGNING_PRINCIPAL = "ai-agent-engine-owner"
SIGNING_NAMESPACE = "ai-agent-engine-release@vl-ktk"
SIGNERS_SHA256 = "fdbc9ac1009f339bf11e1f2de894bdd42efc944befddd41c7f676f1bc87299c2"
INSTALLER_SHA256 = "bdd6a89574ef8fdad65375d4c026a3faf92d8d639b447669898d7bdfc7e39e14"
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
WHEEL = re.compile(r"[A-Za-z0-9_.+-]+\.whl\Z")
MODULE = re.compile(r"ai_agent_engine/[a-zA-Z0-9_]+\.py\Z")


class AddonRefused(RuntimeError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AddonRefused("duplicate JSON key")
        result[key] = value
    return result


def _regular_bytes(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size < 1 or metadata.st_size > limit):
            raise AddonRefused("unsafe artifact")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) != metadata.st_size:
            raise AddonRefused("artifact changed during read")
        return data
    finally:
        os.close(fd)


def _root_file(path: Path, *, mode: int | None = None) -> None:
    parent = path.parent.lstat()
    info = path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
            or parent.st_mode & 0o022 or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1 or info.st_uid != 0
            or info.st_mode & 0o022 or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise AddonRefused("trusted root file changed")


def _current(expected: str) -> None:
    if expected != LIVE_SHA or CURRENT.is_symlink() is False:
        raise AddonRefused("unexpected live release")
    if os.readlink(CURRENT) != "releases/" + LIVE_SHA:
        raise AddonRefused("live release changed")


def _root_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022):
        raise AddonRefused("root staging directory is not trusted")


def _verify_intent(*, source_sha: str, current: str, wheel_sha256: str,
                   incoming: Path = INCOMING, signers: Path = SIGNERS,
                   releases: Path = RELEASES, now: int | None = None) -> None:
    if source_sha != SOURCE_SHA or current != LIVE_SHA or not SHA64.fullmatch(wheel_sha256):
        raise AddonRefused("unapproved add-on candidate")
    directory = incoming / source_sha
    if directory.is_symlink() or not directory.is_dir():
        raise AddonRefused("missing staged artifact")
    raw = _regular_bytes(directory / "release-intent.json", 4096)
    signature = _regular_bytes(directory / "release-intent.json.sig", 4096)
    try:
        doc = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise AddonRefused("invalid intent") from exc
    required = {"schema": 1, "operation": OPERATION, "source_sha": source_sha,
                "expected_current": current, "wheel_sha256": wheel_sha256}
    if (not isinstance(doc, dict) or set(doc) != set(required) | {"issued_at", "expires_at"}
            or any(type(doc.get(k)) is not type(v) or doc.get(k) != v
                   for k, v in required.items())):
        raise AddonRefused("signed intent does not authorize this installation")
    tick = int(time.time()) if now is None else now
    issued, expires = doc["issued_at"], doc["expires_at"]
    if (type(issued) is not int or type(expires) is not int
            or issued > tick + 120 or issued < tick - 1200
            or expires <= tick or expires > issued + 1200):
        raise AddonRefused("expired or invalid owner intent")
    _root_file(signers)
    if hashlib.sha256(_regular_bytes(signers, 4096)).hexdigest() != SIGNERS_SHA256:
        raise AddonRefused("owner signing authority differs from reviewed public key")
    _root_directory(releases)
    with tempfile.TemporaryDirectory(prefix=".addon-signature-", dir=releases) as temp:
        signature_path = Path(temp) / "intent.sig"
        signature_path.write_bytes(signature)
        try:
            subprocess.run(
                ["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(signers),
                 "-I", SIGNING_PRINCIPAL, "-n", SIGNING_NAMESPACE,
                 "-s", str(signature_path)],
                input=raw, check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise AddonRefused("invalid owner signature") from exc


def _wheel_bytes(*, source_sha: str, wheel_sha256: str,
                 incoming: Path = INCOMING) -> bytes:
    directory = incoming / source_sha
    raw = _regular_bytes(directory / "release-manifest.json", 8192)
    try:
        doc = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise AddonRefused("invalid prepared manifest") from exc
    if (not isinstance(doc, dict) or doc.get("schema") != 1
            or doc.get("source_commit") != SOURCE_SHA
            or doc.get("wheel_sha256") != wheel_sha256
            or not isinstance(doc.get("wheel_file"), str)
            or not WHEEL.fullmatch(doc["wheel_file"])):
        raise AddonRefused("prepared manifest does not match signed candidate")
    contents = _regular_bytes(directory / doc["wheel_file"], 100_000_000)
    if hashlib.sha256(contents).hexdigest() != wheel_sha256:
        raise AddonRefused("wheel digest differs from signed authorization")
    return contents


def _extract_package(wheel_bytes: bytes, destination: Path) -> int:
    """Read only inert Python source, never install wheel metadata/scripts."""
    from io import BytesIO
    names = set()
    total_size = 0
    try:
        with zipfile.ZipFile(BytesIO(wheel_bytes)) as wheel:
            for item in wheel.infolist():
                name = item.filename
                if name in names:
                    raise AddonRefused("duplicate wheel member")
                names.add(name)
                if not MODULE.fullmatch(name):
                    continue
                if item.file_size > 500_000 or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise AddonRefused("unsafe Python member")
                total_size += item.file_size
                if total_size > 25_000_000 or len(names) > 1000:
                    raise AddonRefused("wheel contains too much Python source")
                content = wheel.read(item)
                target = destination / name
                target.parent.mkdir(exist_ok=True)
                with target.open("xb") as output:
                    output.write(content)
                target.chmod(0o444)
    except AddonRefused:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise AddonRefused("invalid signed wheel") from exc
    runtime = destination / "ai_agent_engine" / "diagnostic_managed_runtime.py"
    if (len(list((destination / "ai_agent_engine").glob("*.py"))) < 20
            or not (destination / "ai_agent_engine" / "__init__.py").is_file()
            or hashlib.sha256(runtime.read_bytes()).hexdigest() != RUNTIME_SHA256):
        raise AddonRefused("reviewed diagnostic runtime not present in wheel")
    return len(list((destination / "ai_agent_engine").glob("*.py")))


def activate(*, source_sha: str, expected_current: str, wheel_sha256: str) -> None:
    _current(expected_current)
    _root_file(INSTALLER, mode=0o444)
    if hashlib.sha256(_regular_bytes(INSTALLER, 150_000)).hexdigest() != INSTALLER_SHA256:
        raise AddonRefused("trusted reviewed installer changed")
    _verify_intent(source_sha=source_sha, current=expected_current,
                   wheel_sha256=wheel_sha256)
    signed_wheel = _wheel_bytes(source_sha=source_sha, wheel_sha256=wheel_sha256)
    with tempfile.TemporaryDirectory(prefix=".addon-verified-", dir=RELEASES) as temp:
        stage = Path(temp)
        count = _extract_package(signed_wheel, stage)
        if count < 20:
            raise AddonRefused("incomplete reviewed package")
        _current(expected_current)
        EVIDENCE.mkdir(mode=0o700, exist_ok=True)
        info = EVIDENCE.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise AddonRefused("unsafe private release evidence")
        evidence = EVIDENCE / (source_sha + "-" + str(int(time.time())))
        evidence.mkdir(mode=0o700)
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                       "AE_EXPECTED_CURRENT": expected_current,
                       "AE_SOURCE_SHA": source_sha, "AE_REQUEST_SHA": source_sha,
                       "AE_EVIDENCE": str(evidence),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        subprocess.run(["/usr/bin/python3", "-I", str(INSTALLER)], cwd=stage,
                       env=environment, stdin=subprocess.DEVNULL,
                       timeout=180, check=True)
    _current(expected_current)


def main() -> int:
    parser = argparse.ArgumentParser(description="Signed isolated 7.7 diagnostic activation")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--expected-current", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if args.confirmation != CONFIRMATION or os.geteuid() != 0:
        parser.error("owner confirmation and root operator required")
    try:
        activate(source_sha=args.source_sha, expected_current=args.expected_current,
                 wheel_sha256=args.wheel_sha256)
    except (AddonRefused, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print("PASS: signed isolated diagnostic add-on activated; controller release unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
