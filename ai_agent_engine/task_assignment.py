"""Stages 5.6 and 5.9: immutable assignment and private workspace exclusion."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import stat


_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_WORKSPACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class InvalidAssignment(ValueError):
    """Untrusted worker or workspace identifiers are unacceptable."""


class AlreadyAssigned(RuntimeError):
    """The task has an existing immutable worker+workspace binding."""


class WorkspaceBusy(RuntimeError):
    """Another active task/process holds this workspace directory."""


class WorkspaceLease:
    """OS-level exclusive flock on a directory FD; creates no lock files."""

    def __init__(self, directory: str, fd: int) -> None:
        self.directory = directory
        self._fd: int | None = fd

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)  # closing releases flock, including on exceptions


@dataclass(frozen=True)
class TaskAssignment:
    task_id: str
    worker_id: str
    workspace_name: str
    workspace_dir: str

    def metadata(self) -> dict[str, str]:
        """No prompts, credentials or filesystem contents are returned."""
        return {"task_id": self.task_id, "worker_id": self.worker_id,
                "workspace_name": self.workspace_name,
                "workspace_dir": self.workspace_dir}


def validate_worker(worker_id: str) -> None:
    if type(worker_id) is not str or not _WORKER_ID.fullmatch(worker_id):
        raise InvalidAssignment("invalid worker identifier")


def validate_workspace_name(name: str) -> None:
    if type(name) is not str or not _WORKSPACE_NAME.fullmatch(name):
        raise InvalidAssignment("invalid workspace name")


class WorkspaceRoot:
    """Trusted private root; only pre-existing private child directories."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).absolute()
        self._check_private_dir(self.path)

    @staticmethod
    def _check_private_dir(path: Path) -> None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise InvalidAssignment("workspace directory is unavailable") from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise InvalidAssignment("workspace directory must be private 0700")

    def resolve(self, name: str) -> str:
        validate_workspace_name(name)
        self._check_private_dir(self.path)  # fail closed if root changed
        candidate = self.path / name
        self._check_private_dir(candidate)  # lstat rejects symlinks
        return str(candidate)

    def acquire(self, name: str) -> WorkspaceLease:
        """Reserve the existing directory across threads and OS processes.

        Locking the directory inode itself avoids extra files, permissions
        changes and cleanup. This requires POSIX directory flock (Linux).
        """
        directory = self.resolve(name)
        try:
            expected = os.lstat(directory)
        except OSError as exc:
            raise InvalidAssignment("workspace directory disappeared") from exc
        flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(directory, flags)
        except OSError as exc:
            raise InvalidAssignment("workspace directory cannot be opened") from exc
        try:
            actual = os.fstat(fd)
            self._check_private_dir(self.path)
            if (not stat.S_ISDIR(actual.st_mode)
                    or actual.st_uid != os.geteuid()
                    or stat.S_IMODE(actual.st_mode) != 0o700
                    or (actual.st_dev, actual.st_ino) !=
                       (expected.st_dev, expected.st_ino)):
                raise InvalidAssignment("workspace directory changed during claim")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceBusy("workspace already reserved") from exc
            except OSError as exc:
                raise InvalidAssignment("workspace filesystem cannot be locked") from exc
            return WorkspaceLease(directory, fd)
        except BaseException:
            os.close(fd)
            raise

    def verify_loaded(self, name: str, directory: str) -> None:
        """Do not silently rebind a saved assignment to a different root."""
        if type(directory) is not str or self.resolve(name) != directory:
            raise InvalidAssignment("persisted workspace outside configured root")
