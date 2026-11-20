"""Owner-gated Netcup backup-hook metadata audit, without executing the hook.

No SQLite access, backup access, service mutation, secrets, or source disclosure.
The only content read is the installed fixed-path script, for SHA-256 comparison
after strict type, owner, mode and hardlink validation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


LIVE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
CURRENT = Path("/opt/ai-agent-engine/current")
HOOK = Path("/usr/local/libexec/ae-diagnostic-backup.py")
EXPECTED_HOOK_SHA256 = "6ef4e3d0de703a20b6ffaee1968ad78f8dcef022283809e32466d72bdc94304d"
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")
MAX_HOOK_BYTES = 128 * 1024


def _metadata(path: Path, *, uid: int = 0, expected_hash: str = EXPECTED_HOOK_SHA256) -> dict:
    """Return bounded findings; refuse symlinks and don't print unknown bytes."""
    result = {
        "presence": "not_checked", "file_type": "not_checked",
        "owner_expected": False, "mode_expected": False,
        "single_link": False, "size_safe": False,
        "sha256_check": "not_checked", "diagnosis": "unknown",
    }
    try:
        before = path.lstat()
    except FileNotFoundError:
        result.update(presence="missing", file_type="absent", diagnosis="missing")
        return result
    except OSError:
        result.update(presence="unavailable", file_type="unknown", diagnosis="stat_unavailable")
        return result

    result["presence"] = "present"
    if stat.S_ISLNK(before.st_mode):
        result.update(file_type="symlink", diagnosis="symlink_refused")
        return result
    if not stat.S_ISREG(before.st_mode):
        result.update(file_type="other", diagnosis="wrong_type")
        return result

    result.update(
        file_type="regular",
        owner_expected=before.st_uid == uid,
        mode_expected=stat.S_IMODE(before.st_mode) == 0o755,
        single_link=before.st_nlink == 1,
        size_safe=0 < before.st_size <= MAX_HOOK_BYTES,
    )
    for key, diagnosis in (
        ("owner_expected", "wrong_owner"), ("mode_expected", "wrong_mode"),
        ("single_link", "unexpected_hardlinks"), ("size_safe", "size_out_of_bounds"),
    ):
        if not result[key]:
            result["diagnosis"] = diagnosis
            return result

    # O_NOFOLLOW prevents leaf-symlink substitution, and the pre/post fstat
    # checks reject a file swapped or modified during the bounded hash read.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        result["diagnosis"] = "open_refused"
        return result
    with os.fdopen(fd, "rb") as source:
        start = os.fstat(source.fileno())
        if (start.st_dev, start.st_ino, start.st_size, start.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ):
            result["diagnosis"] = "changed_before_read"
            return result
        digest = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: source.read(32768), b""):
            total += len(chunk)
            if total > MAX_HOOK_BYTES:
                result["diagnosis"] = "changed_during_read"
                return result
            digest.update(chunk)
        after = os.fstat(source.fileno())
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            start.st_dev, start.st_ino, start.st_size, start.st_mtime_ns
        ) or total != start.st_size:
            result["diagnosis"] = "changed_during_read"
            return result

    matched = digest.hexdigest() == expected_hash
    result["sha256_check"] = "match" if matched else "mismatch"
    result["diagnosis"] = "reviewed_hook_match" if matched else "hook_hash_mismatch"
    return result


def main() -> None:
    if os.geteuid() != 0 or not all(os.environ.get(name) for name in (
        "AE_SOURCE_SHA", "AE_REQUEST_SHA", "AE_EVIDENCE"
    )):
        raise RuntimeError("authenticated owner-admin metadata audit required")
    expected = os.environ.get("AE_EXPECTED_CURRENT")
    if expected != LIVE_SHA or os.readlink(CURRENT) != "releases/" + expected:
        raise RuntimeError("Netcup live release changed; HOLD")
    for unit in UNITS:
        if subprocess.run(
            ("/usr/bin/systemctl", "is-active", "--quiet", unit),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=8, check=False,
        ).returncode:
            raise RuntimeError("baseline unit inactive; HOLD")

    hook = _metadata(HOOK)
    report = {
        "schema": 1, "scope": "vl-ktk/ai-agent-engine",
        "live_release": expected, "baseline_units_active": True,
        "hook": hook,
        "hook_executed": False, "sqlite_or_backup_opened": False,
        "working_data_modified": False, "service_changed": False,
        "status": "INVENTORIED_ONLY_HOLD_NO_CHANGE",
    }
    evidence = Path(os.environ["AE_EVIDENCE"])
    meta = evidence.lstat()
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0
            or stat.S_IMODE(meta.st_mode) != 0o700):
        raise RuntimeError("unsafe private evidence directory")
    with (evidence / "backup-hook-metadata-7-11.json").open("x", encoding="utf-8") as dest:
        json.dump(report, dest, separators=(",", ":"), sort_keys=True)
        dest.write("\n")
    print("BACKUP_HOOK_METADATA_7_11 " + json.dumps(
        report, separators=(",", ":"), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
