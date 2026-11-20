"""Separate, private snapshots for the first 7.7 diagnostic project.

Standalone stdlib-only script. It never reads the existing controller data or
backups, changes live SQLite, deploys services or sends data off-host.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
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


SOURCE = Path("/var/lib/ai-agent-engine-diagnostics")
DESTINATION = Path("/var/backups/ai-agent-engine-diagnostics")
DATABASES = {
    "tasks.sqlite3": (0x4145494E, 14),
    "projects/project-leaders.sqlite3": (0x41454C52, 1),
    "projects/vl-ktk/ai-agent-engine/leader.sqlite3": (0x41454C50, 2),
}
PROJECT = "vl-ktk/ai-agent-engine"
PROPOSAL_ID = "proposal_8950a54bb02d4537943c5ed6f3286696"
PROPOSAL_SHA = "8443ee2fd8de2cb0c893a39420b0229b463a055b014f3bf93c571952f573dd56"
_NAME = re.compile(r"20\d{6}T\d{6}\.\d{6}Z-[0-9a-f]{8}\Z")
_DB_SIDECARS = ("-wal", "-shm", "-journal")
MAX_BYTES = 512 * 1024 * 1024
KEEP = 7


class DiagnosticBackupError(Exception):
    """Non-data-bearing failure suitable for privileged logs."""


def _private_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise DiagnosticBackupError("missing private directory") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or path.is_symlink()):
        raise DiagnosticBackupError("unsafe private directory")


def _regular(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise DiagnosticBackupError("missing private file") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise DiagnosticBackupError("unsafe private file")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_check(path: Path, app: int, version: int) -> None:
    _regular(path)
    with sqlite3.connect("file:" + str(path) + "?mode=ro&immutable=1", uri=True) as db:
        if ((db.execute("PRAGMA application_id").fetchone()[0],
             db.execute("PRAGMA user_version").fetchone()[0]) != (app, version)
                or db.execute("PRAGMA quick_check").fetchone() != ("ok",)):
            raise DiagnosticBackupError("unexpected diagnostic database")


def _archive_members(archive: Path, *, budget: int) -> tuple[int, int]:
    files, size = 0, 0
    seen = set()
    with tarfile.open(archive, "r:gz") as tar:
        for item in tar:
            path = PurePosixPath(item.name)
            if (item.name in seen or path.is_absolute() or ".." in path.parts or not path.parts
                    or not (item.isfile() or item.isdir())
                    or item.name in DATABASES
                    or item.name.endswith((".sqlite3", ".sqlite3-wal",
                                           ".sqlite3-shm", ".sqlite3-journal"))
                    or item.name.startswith("/") or path.parts[0] not in {
                        "runtime.json", "projects", "workspaces"}):
                raise DiagnosticBackupError("unexpected archive member")
            seen.add(item.name)
            if item.isfile():
                files += 1
                size += item.size
                if size > budget:
                    raise DiagnosticBackupError("diagnostic archive exceeds budget")
                stream = tar.extractfile(item)
                if stream is None:
                    raise DiagnosticBackupError("unreadable archive member")
                with stream:
                    while stream.read(1024 * 1024):
                        pass
    return files, size


class DiagnosticBackup:
    def __init__(self, source: Path = SOURCE, destination: Path = DESTINATION,
                 *, max_bytes: int = MAX_BYTES, keep: int = KEEP):
        if (os.name != "posix" or not source.is_absolute() or not destination.is_absolute()
                or max_bytes < 1024 or keep < 1):
            raise DiagnosticBackupError("invalid backup scope")
        self.source = source
        self.destination = destination
        self.max_bytes = max_bytes
        self.keep = keep

    @contextmanager
    def _lock(self):
        _private_dir(self.destination)
        fd = os.open(self.destination / ".backup.lock",
                     os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise DiagnosticBackupError("unsafe backup lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _check_source(self) -> list[tuple[Path, str]]:
        _private_dir(self.source)
        config = self.source / "runtime.json"
        _regular(config)
        doc = json.loads(config.read_text(encoding="utf-8"))
        if (doc.get("owner_id") != "vl-ktk" or doc.get("project_ids") != ["ai-agent-engine"]
                or doc.get("task_db") != str(self.source / "tasks.sqlite3")):
            raise DiagnosticBackupError("wrong diagnostic scope")
        for name, (app, version) in DATABASES.items():
            _database_check(self.source / name, app, version)
        registry = self.source / "projects/project-leaders.sqlite3"
        with sqlite3.connect("file:" + str(registry) + "?mode=ro&immutable=1", uri=True) as db:
            rows = db.execute("SELECT owner_id,project_id,proposal_id,proposal_sha256,state "
                              "FROM projects").fetchall()
            if rows != [("vl-ktk", "ai-agent-engine", PROPOSAL_ID, PROPOSAL_SHA, "active")]:
                raise DiagnosticBackupError("unexpected project approval")
        with sqlite3.connect("file:" + str(self.source / "tasks.sqlite3")
                             + "?mode=ro&immutable=1", uri=True) as db:
            foreign = db.execute("SELECT COUNT(*) FROM tasks WHERE owner_id<>? OR project_id<>?",
                                 ("vl-ktk", "ai-agent-engine")).fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM task_states "
                                "WHERE state IN ('assigned','running')").fetchone()[0]
            deliveries = db.execute("SELECT COUNT(*) FROM task_deliveries "
                                    "WHERE status IN ('active','retryable')").fetchone()[0]
            if foreign or active or deliveries:
                raise DiagnosticBackupError("task runtime not quiescent")

        files: list[tuple[Path, str]] = []
        budget = 0
        for path in sorted(self.source.rglob("*")):
            relative = path.relative_to(self.source).as_posix()
            if relative in DATABASES:
                continue
            if any(relative == db + suffix for db in DATABASES
                   for suffix in _DB_SIDECARS):
                raise DiagnosticBackupError("database has pending sidecar")
            info = path.lstat()
            if (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                    or info.st_uid != os.geteuid() or (info.st_mode & 0o022)
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise DiagnosticBackupError("unsafe diagnostic tree")
            if stat.S_ISREG(info.st_mode):
                budget += info.st_size
                if budget > self.max_bytes:
                    raise DiagnosticBackupError("diagnostic source exceeds budget")
            files.append((path, relative))
        return files

    def _snapshots(self) -> list[Path]:
        snapshots = []
        for path in self.destination.iterdir():
            if _NAME.fullmatch(path.name):
                _private_dir(path)
                snapshots.append(path)
        return sorted(snapshots)

    def verify(self, snapshot: Path) -> dict:
        if snapshot.parent != self.destination or not _NAME.fullmatch(snapshot.name):
            raise DiagnosticBackupError("snapshot outside private backup root")
        _private_dir(snapshot)
        manifest_path = snapshot / "manifest.json"
        archive = snapshot / "files.tar.gz"
        _regular(manifest_path)
        _regular(archive)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("schema") != 1 or manifest.get("snapshot") != snapshot.name
                or manifest.get("scope") != PROJECT
                or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("config_sha256", "")))
                or manifest.get("archive_sha256") != _hash(archive)):
            raise DiagnosticBackupError("diagnostic manifest mismatch")
        file_count, _ = _archive_members(archive, budget=self.max_bytes)
        if manifest.get("file_count") != file_count:
            raise DiagnosticBackupError("archive count mismatch")
        records = manifest.get("databases")
        if type(records) is not dict or set(records) != set(DATABASES):
            raise DiagnosticBackupError("database manifest mismatch")
        for name, (app, version) in DATABASES.items():
            path = snapshot / name.replace("/", "__")
            _database_check(path, app, version)
            if records[name] != {"sha256": _hash(path), "bytes": path.stat().st_size}:
                raise DiagnosticBackupError("database digest mismatch")
        return manifest

    def create(self) -> tuple[str, int]:
        old_umask = os.umask(0o077)
        try:
            with self._lock():
                files = self._check_source()
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                name = stamp + "-" + secrets.token_hex(4)
                final = self.destination / name
                if final.exists() or final.is_symlink():
                    raise DiagnosticBackupError("snapshot collision")
                with tempfile.TemporaryDirectory(prefix=".partial-", dir=self.destination) as tmp:
                    staging = Path(tmp)
                    dbs = {}
                    for relative, (app, version) in DATABASES.items():
                        target = staging / relative.replace("/", "__")
                        with sqlite3.connect("file:" + str(self.source / relative)
                                             + "?mode=ro", uri=True) as source:
                            with sqlite3.connect(target) as dest:
                                source.backup(dest, pages=128, sleep=0.05)
                        _database_check(target, app, version)
                        dbs[relative] = {"sha256": _hash(target), "bytes": target.stat().st_size}
                    archive = staging / "files.tar.gz"
                    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
                        for path, arcname in files:
                            tar.add(path, arcname=arcname, recursive=False)
                    file_count, _ = _archive_members(archive, budget=self.max_bytes)
                    if archive.stat().st_size + sum(item["bytes"] for item in dbs.values()) > self.max_bytes:
                        raise DiagnosticBackupError("diagnostic backup exceeds budget")
                    manifest = {"schema": 1, "scope": PROJECT, "snapshot": name,
                                "databases": dbs, "archive_sha256": _hash(archive),
                                "config_sha256": _hash(self.source / "runtime.json"),
                                "file_count": file_count}
                    (staging / "manifest.json").write_text(
                        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
                    # Same strict verification as a published snapshot.
                    staging.rename(final)
                self.verify(final)
                existing = self._snapshots()
                for older in existing[:-self.keep]:
                    self.verify(older)  # never delete an unverified backup
                    shutil.rmtree(older)
                return name, len(self._snapshots())
        finally:
            os.umask(old_umask)

    def newest(self) -> dict:
        with self._lock():
            snapshots = self._snapshots()
            if not snapshots:
                raise DiagnosticBackupError("no diagnostic backup exists")
            return self.verify(snapshots[-1])

    def restore_smoke(self, snapshot: Path) -> dict:
        """Recover only into a new disposable directory and verify all DBs."""
        manifest = self.verify(snapshot)
        with tempfile.TemporaryDirectory(prefix="ae-diag-restore-") as tmp:
            destination = Path(tmp)
            with tarfile.open(snapshot / "files.tar.gz", "r:gz") as tar:
                tar.extractall(destination, filter="data")
            for name, (app, version) in DATABASES.items():
                recovered = destination / name
                recovered.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(snapshot / name.replace("/", "__"), recovered)
                _database_check_recovered(recovered, app, version)
            if _hash(destination / "runtime.json") != manifest["config_sha256"]:
                raise DiagnosticBackupError("recovered runtime configuration mismatch")
        return {"status": "RESTORE_SMOKE_PASS", "scope": PROJECT,
                "snapshot": manifest["snapshot"], "database_count": len(DATABASES)}


def _database_check_recovered(path: Path, app: int, version: int) -> None:
    with sqlite3.connect("file:" + str(path) + "?mode=ro&immutable=1", uri=True) as db:
        if ((db.execute("PRAGMA application_id").fetchone()[0],
             db.execute("PRAGMA user_version").fetchone()[0]) != (app, version)
                or db.execute("PRAGMA quick_check").fetchone() != ("ok",)):
            raise DiagnosticBackupError("recovered SQLite integrity failed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "verify", "restore-smoke"))
    args = parser.parse_args(argv)
    backup = DiagnosticBackup()
    try:
        if args.action == "create":
            snapshot, retained = backup.create()
            print(f"diagnostic backup PASS snapshot={snapshot} retained={retained}")
        elif args.action == "verify":
            report = backup.newest()
            print("diagnostic backup verify PASS snapshot=" + report["snapshot"])
        else:
            snapshots = backup._snapshots()
            if not snapshots:
                raise DiagnosticBackupError("missing snapshot")
            result = backup.restore_smoke(snapshots[-1])
            print(result["status"])
        return 0
    except (DiagnosticBackupError, OSError, sqlite3.Error, tarfile.TarError,
            ValueError, KeyError, json.JSONDecodeError):
        print("diagnostic backup HOLD (private evidence required)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
