"""Read-only Linux credential files for the single-owner server runtime.

The writer is the host administrator, outside the engine and its model tools.
There is intentionally no write, list-names, export or print-secret command.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import tempfile


DEFAULT_DIRECTORY = Path("/etc/ai-agent-engine/secrets")
MAX_SECRET_BYTES = 65_536
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z", re.ASCII)
_DIRECTORY_MODE = 0o750
_FILE_MODE = 0o440


class SecretAccessError(Exception):
    """Generic failure: never include a secret value, name or absolute path."""

    def __init__(self) -> None:
        super().__init__("Credential store unavailable or access denied")


@dataclass(frozen=True)
class SecretStoreStatus:
    file_count: int


class SecretStore:
    """Read immutable root-managed files using directory-relative descriptors.

    Production defaults enforce root ownership and the existing ae-runtime group.
    Ownership overrides are only for isolated tests; callers with direct filesystem
    access already have their OS privileges, not a Python security boundary.
    """

    def __init__(
        self,
        root: Path = DEFAULT_DIRECTORY,
        *,
        owner_uid: int = 0,
        reader_gid: int | None = None,
        verify_ancestors: bool = True,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise SecretAccessError() from None
        if reader_gid is None:
            try:
                import grp

                reader_gid = grp.getgrnam("ae-runtime").gr_gid
            except (KeyError, ImportError, OSError):
                raise SecretAccessError() from None
        if (not isinstance(root, Path) or not root.is_absolute()
                or not isinstance(owner_uid, int) or owner_uid < 0
                or not isinstance(reader_gid, int) or reader_gid < 0):
            raise SecretAccessError() from None
        self._root = root
        self._owner_uid = owner_uid
        self._reader_gid = reader_gid
        self._verify_ancestors = verify_ancestors

    @staticmethod
    def _valid_name(name: str) -> bool:
        return isinstance(name, str) and _NAME.fullmatch(name) is not None

    def _check_parent_chain(self) -> None:
        """Make sure an attacker cannot swap a writable parent or a symlink."""
        if not self._verify_ancestors:
            return
        path = self._root.parent
        while True:
            metadata = os.lstat(path)
            if (not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != 0
                    or (stat.S_IMODE(metadata.st_mode) & 0o022)):
                raise SecretAccessError()
            if path == path.parent:
                break
            path = path.parent

    def _open_directory(self) -> int:
        self._check_parent_chain()
        fd = os.open(
            self._root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != self._owner_uid
                    or metadata.st_gid != self._reader_gid
                    or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE):
                raise SecretAccessError()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_file(self, directory_fd: int, name: str) -> int:
        if not self._valid_name(name):
            raise SecretAccessError()
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != self._owner_uid
                    or metadata.st_gid != self._reader_gid
                    or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
                    or not 0 < metadata.st_size <= MAX_SECRET_BYTES):
                raise SecretAccessError()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, name: str) -> bytes:
        """Return bytes only to an explicitly trusted runtime caller.

        No file contents appear in exceptions or logs; never use this method
        as a model-exposed tool or a public API endpoint.
        """
        try:
            directory_fd = self._open_directory()
            try:
                file_fd = self._open_file(directory_fd, name)
                try:
                    with os.fdopen(file_fd, "rb") as source:
                        data = source.read(MAX_SECRET_BYTES + 1)
                    if not 0 < len(data) <= MAX_SECRET_BYTES:
                        raise SecretAccessError()
                    return data
                except BaseException:
                    # fdopen owns the descriptor after its successful creation.
                    raise
            finally:
                os.close(directory_fd)
        except (OSError, SecretAccessError, ValueError, TypeError):
            raise SecretAccessError() from None

    def audit(self) -> SecretStoreStatus:
        """Verify every file's metadata; disclose only the total count."""
        try:
            directory_fd = self._open_directory()
            try:
                names = os.listdir(directory_fd)
                for name in names:
                    file_fd = self._open_file(directory_fd, name)
                    os.close(file_fd)
                return SecretStoreStatus(file_count=len(names))
            finally:
                os.close(directory_fd)
        except (OSError, SecretAccessError, ValueError, TypeError):
            raise SecretAccessError() from None


def _disposable_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="ae-secrets-smoke-") as temp:
        root = Path(temp) / "secret-files"
        root.mkdir(mode=_DIRECTORY_MODE)
        secret = root / "synthetic"
        sentinel = b"synthetic-placeholder-not-a-real-token"
        secret.write_bytes(sentinel)
        secret.chmod(_FILE_MODE)
        store = SecretStore(
            root, owner_uid=os.getuid(), reader_gid=os.getgid(),
            verify_ancestors=False,
        )
        if store.audit().file_count != 1 or store.read("synthetic") != sentinel:
            raise SecretAccessError()
        secret.chmod(0o444)
        try:
            store.read("synthetic")
        except SecretAccessError:
            pass
        else:
            raise SecretAccessError()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private credential store metadata check")
    parser.add_argument("action", choices=("audit", "smoke"))
    options = parser.parse_args(argv)
    try:
        if options.action == "smoke":
            _disposable_smoke()
            print("Credential store disposable smoke: PASS")
        else:
            status = SecretStore().audit()
            print(f"Credential store secure metadata: PASS ({status.file_count} files)")
        return 0
    except SecretAccessError:
        print("Credential store check: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
