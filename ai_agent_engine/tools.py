"""Constrained filesystem tools for an existing private working directory.

On Linux, each access is anchored to the original directory inode and uses
dir_fd + O_NOFOLLOW. This is a file-TOOL boundary, not a process/OS sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
from typing import Mapping

from .core import Tool


class ToolError(Exception):
    """Expected tool rejection. Its message is intentionally not included in audit."""


class SandboxFiles:
    def __init__(self, root: Path, *, max_bytes: int = 8192):
        root = Path(root).absolute()
        try:
            root_info = os.lstat(root)
        except OSError:
            raise ValueError("sandbox root must already exist") from None
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError("sandbox root must already exist")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise ValueError("invalid max_bytes")
        self._root = root.resolve(strict=True)
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        self._max_bytes = max_bytes

    def _target(self, path_text: str) -> Path:
        if (type(path_text) is not str or not path_text
                or "\\" in path_text or "\x00" in path_text or ":" in path_text):
            raise ToolError("invalid path")
        try:
            encoded = path_text.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ToolError("invalid path") from None
        if len(encoded) > 512:
            raise ToolError("invalid path")
        path = PurePosixPath(path_text)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path_text.split("/")):
            raise ToolError("path escapes sandbox")
        try:
            resolved = (self._root / path_text).resolve(strict=False)
        except (OSError, RuntimeError):
            raise ToolError("invalid path") from None
        if not resolved.is_relative_to(self._root) or resolved == self._root:
            raise ToolError("path escapes sandbox")
        current = self._root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ToolError("symlinks are not permitted")
        return resolved

    def _open_file(self, path_text: str, flags: int) -> int:
        """Open below the root without following path symlinks on POSIX.

        Windows lacks dir_fd/openat support; its fallback relies on the preflight
        path checks and a private directory controlled by the same OS account.
        """
        target = self._target(path_text)
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            parts = PurePosixPath(path_text).parts
            try:
                descriptor = os.open(
                    self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError:
                raise ToolError("sandbox root unavailable") from None
            try:
                info = os.fstat(descriptor)
                if ((info.st_dev, info.st_ino) != self._root_identity
                        or not stat.S_ISDIR(info.st_mode)):
                    raise ToolError("sandbox root changed")
                for part in parts[:-1]:
                    next_descriptor = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = next_descriptor
                return os.open(
                    parts[-1], flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600, dir_fd=descriptor,
                )
            finally:
                os.close(descriptor)
        return os.open(target, flags, 0o600)

    def write_new(self, args: Mapping[str, str]) -> str:
        path_text = args.get("path", "")
        file_path = self._target(path_text)
        content = args.get("content", "")
        if not isinstance(content, str):
            raise ToolError("content exceeds size limit")
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ToolError("invalid UTF-8 content") from None
        if len(encoded) > self._max_bytes:
            raise ToolError("content exceeds size limit")
        if not file_path.parent.is_dir():
            raise ToolError("parent directory does not exist")
        try:
            with os.fdopen(self._open_file(path_text, os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                           "wb") as handle:
                handle.write(encoded)
        except FileExistsError as exc:
            raise ToolError("refusing to overwrite") from exc
        except OSError:
            raise ToolError("file unavailable") from None
        return f"Created {file_path.name} ({len(encoded)} bytes)"

    def read_text(self, args: Mapping[str, str]) -> str:
        path_text = args.get("path", "")
        try:
            with os.fdopen(self._open_file(path_text, os.O_RDONLY), "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ToolError("file unavailable or too large")
                data = handle.read(self._max_bytes + 1)
            if len(data) > self._max_bytes:
                raise ToolError("file unavailable or too large")
            return data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolError("file unavailable or invalid UTF-8") from exc

    def allowed_tools(self) -> tuple[Tool, ...]:
        return (
            Tool("write_new_file", self.write_new, required_args=("path", "content")),
            Tool("read_file", self.read_text, required_args=("path",)),
        )
