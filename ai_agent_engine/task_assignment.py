"""Stage 5.6: immutable worker+workspace binding, without worker execution."""

from __future__ import annotations

from dataclasses import dataclass
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

    def verify_loaded(self, name: str, directory: str) -> None:
        """Do not silently rebind a saved assignment to a different root."""
        if type(directory) is not str or self.resolve(name) != directory:
            raise InvalidAssignment("persisted workspace outside configured root")
