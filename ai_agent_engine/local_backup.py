"""Small local, private and verifiable backups for the owner-operated VPS.

No upload, restore, secret-directory access, or externally chosen source paths.
The installed standalone copy is owned by root and executed as ``ae-runtime``.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from typing import Iterator

if os.name == "posix":
    import fcntl


DATA_ROOT = Path("/var/lib/ai-agent-engine")
BACKUP_ROOT = Path("/var/backups/ai-agent-engine")
SOURCE_DIRS = ("projects", "workspaces", "artifacts")
DB_NAME = "engine.sqlite3"
MAX_BACKUP_BYTES = 2 * 1024 ** 3
KEEP_SNAPSHOTS = 7
_SNAPSHOT = re.compile(r"20\d{6}T\d{6}\.\d{6}Z-[0-9a-f]{8}\Z")
_DB_SIDECARS = {DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal"}


class BackupError(Exception):
    """A deliberately non-diagnostic public error: no file names or content."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_dir(path: Path, *, owner: int | None = None) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != (os.getuid() if owner is None else owner)):
        raise BackupError()


def _check_regular(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise BackupError()


def _validate_archive(archive: Path, max_bytes: int) -> int:
    count = 0
    uncompressed_bytes = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if (not parts or parts[0] not in SOURCE_DIRS or ".." in parts
                    or member.name.startswith("/")
                    or not (member.isfile() or member.isdir())):
                raise BackupError()
            if member.isfile():
                count += 1
                uncompressed_bytes += member.size
                if uncompressed_bytes > max_bytes:
                    raise BackupError()
                content = tar.extractfile(member)
                if content is None:
                    raise BackupError()
                with content:
                    while content.read(1024 * 1024):
                        pass
    return count


def _check_db(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupError()
    finally:
        connection.close()


class LocalBackup:
    """Creates one complete snapshot, verifies it, then rotates old snapshots."""

    def __init__(self, data_root: Path = DATA_ROOT, backup_root: Path = BACKUP_ROOT,
                 *, keep: int = KEEP_SNAPSHOTS, max_bytes: int = MAX_BACKUP_BYTES):
        if (os.name != "posix" or not data_root.is_absolute() or not backup_root.is_absolute()
                or keep < 1 or not isinstance(max_bytes, int) or max_bytes < 1024):
            raise BackupError()
        self.data_root = data_root
        self.backup_root = backup_root
        self.keep = keep
        self.max_bytes = max_bytes

    @contextmanager
    def _lock(self) -> Iterator[None]:
        _check_dir(self.backup_root)
        lock = self.backup_root / ".backup.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise BackupError()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _snapshots(self) -> list[Path]:
        items: list[Path] = []
        for candidate in self.backup_root.iterdir():
            if _SNAPSHOT.fullmatch(candidate.name):
                _check_dir(candidate)
                items.append(candidate)
        return sorted(items, key=lambda path: path.name)

    def _scan_source(self) -> list[tuple[Path, str]]:
        paths: list[tuple[Path, str]] = []
        source_bytes = 0
        for name in SOURCE_DIRS:
            base = self.data_root / name
            _check_dir(base)
            paths.append((base, name))
            for path in sorted(base.rglob("*")):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise BackupError()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise BackupError()
                if info.st_uid != os.getuid():
                    raise BackupError()
                if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o022:
                    raise BackupError()
                if stat.S_ISREG(info.st_mode):
                    if name == "projects" and path.parent == base and path.name in _DB_SIDECARS:
                        continue
                    source_bytes += info.st_size
                    if source_bytes > self.max_bytes:
                        raise BackupError()
                paths.append((path, f"{name}/{path.relative_to(base).as_posix()}"))
        return paths

    def verify(self, snapshot: Path) -> int:
        if snapshot.parent != self.backup_root or not _SNAPSHOT.fullmatch(snapshot.name):
            raise BackupError()
        _check_dir(snapshot)
        db = snapshot / DB_NAME
        archive = snapshot / "files.tar.gz"
        manifest_path = snapshot / "manifest.json"
        for path in (db, archive, manifest_path):
            _check_regular(path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("version") != 1 or manifest.get("snapshot") != snapshot.name
                or manifest.get("db_sha256") != _sha256(db)
                or manifest.get("archive_sha256") != _sha256(archive)
                or manifest.get("db_bytes") != db.stat().st_size
                or manifest.get("archive_bytes") != archive.stat().st_size):
            raise BackupError()
        _check_db(db)
        if manifest.get("file_count") != _validate_archive(archive, self.max_bytes):
            raise BackupError()
        return db.stat().st_size + archive.stat().st_size + manifest_path.stat().st_size

    def _rotate(self) -> int:
        snapshots = self._snapshots()
        sizes = {p.name: self.verify(p) for p in snapshots}
        total = sum(sizes.values())
        while len(snapshots) > self.keep or total > self.max_bytes:
            if len(snapshots) <= 1:
                raise BackupError()
            old = snapshots.pop(0)
            # Only our validated snapshot directories underneath the dedicated root.
            _check_dir(old)
            shutil.rmtree(old)
            total -= sizes[old.name]
        return len(snapshots)

    def create(self) -> tuple[str, int, int]:
        old_umask = os.umask(0o077)
        try:
            with self._lock():
                # Validate all sources before creating anything new.
                paths = self._scan_source()
                database = self.data_root / "projects" / DB_NAME
                _check_regular(database)
                # Include microseconds so multiple manual runs in one second
                # retain their true creation order during rotation.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                name = stamp + "-" + secrets.token_hex(4)
                destination = self.backup_root / name
                with tempfile.TemporaryDirectory(prefix=".partial-", dir=self.backup_root) as tmp:
                    staging = Path(tmp)
                    db = staging / DB_NAME
                    source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
                    target = sqlite3.connect(db)
                    try:
                        source.backup(target, pages=128, sleep=0.05)
                    finally:
                        target.close()
                        source.close()
                    _check_db(db)
                    archive = staging / "files.tar.gz"
                    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
                        for path, arcname in paths:
                            # No symlinks or non-regular files may enter the archive.
                            info = path.lstat()
                            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                                raise BackupError()
                            tar.add(path, arcname=arcname, recursive=False)
                    count = _validate_archive(archive, self.max_bytes)
                    manifest = {
                        "version": 1, "snapshot": name,
                        "db_sha256": _sha256(db), "archive_sha256": _sha256(archive),
                        "db_bytes": db.stat().st_size,
                        "archive_bytes": archive.stat().st_size,
                        "file_count": count,
                    }
                    (staging / "manifest.json").write_text(
                        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
                    if self.verify_staging(staging, name) > self.max_bytes:
                        raise BackupError()
                    # Replace an absent path only. Generated random name makes collision remote.
                    if destination.exists() or destination.is_symlink():
                        raise BackupError()
                    staging.rename(destination)
                    # TemporaryDirectory sees a missing old path after the move.
                size = self.verify(destination)
                retained = self._rotate()
                return name, size, retained
        except (OSError, sqlite3.Error, tarfile.TarError, ValueError, KeyError,
                json.JSONDecodeError, BlockingIOError):
            raise BackupError() from None
        finally:
            os.umask(old_umask)

    def verify_staging(self, staging: Path, expected_name: str) -> int:
        # Atomic promotion has not happened yet, so the path name is temporary.
        _check_dir(staging)
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("snapshot") != expected_name:
            raise BackupError()
        db, archive = staging / DB_NAME, staging / "files.tar.gz"
        for path in (db, archive, staging / "manifest.json"):
            _check_regular(path)
        if manifest["db_sha256"] != _sha256(db) or manifest["archive_sha256"] != _sha256(archive):
            raise BackupError()
        _check_db(db)
        if manifest["file_count"] != _validate_archive(archive, self.max_bytes):
            raise BackupError()
        return sum(path.stat().st_size for path in (db, archive, staging / "manifest.json"))

    def newest(self) -> tuple[int, int]:
        with self._lock():
            snapshots = self._snapshots()
            if not snapshots:
                raise BackupError()
            return len(snapshots), self.verify(snapshots[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private local AI Agent Engine backup")
    parser.add_argument("action", choices=("create", "verify"))
    args = parser.parse_args(argv)
    try:
        backup = LocalBackup()
        if args.action == "create":
            snapshot, size, retained = backup.create()
            print(f"backup PASS snapshot={snapshot} bytes={size} retained={retained}")
        else:
            retained, size = backup.newest()
            print(f"backup verify PASS bytes={size} retained={retained}")
        return 0
    except (BackupError, OSError):
        print("backup FAILED (check private host diagnostics)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
