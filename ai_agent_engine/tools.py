"""Constrained filesystem tools for an isolated test directory only."""

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
        if not root.is_dir():
            raise ValueError("sandbox root must already exist")
        if max_bytes < 1 or max_bytes > 65536:
            raise ValueError("invalid max_bytes")
        self._root = root.resolve(strict=True)
        self._max_bytes = max_bytes

    def _target(self, path_text: str) -> Path:
        if (not isinstance(path_text, str) or not path_text
                or len(path_text.encode("utf-8", errors="replace")) > 512
                or "\\" in path_text or "\x00" in path_text or ":" in path_text):
            raise ToolError("invalid path")
        path = PurePosixPath(path_text)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path_text.split("/")):
            raise ToolError("path escapes sandbox")
        resolved = (self._root / path_text).resolve(strict=False)
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
            descriptor = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                for part in parts[:-1]:
                    next_descriptor = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = next_descriptor
                return os.open(
                    parts[-1], flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    0o600, dir_fd=descriptor,
                )
            finally:
                os.close(descriptor)
        return os.open(target, flags, 0o600)

    def write_new(self, args: Mapping[str, str]) -> str:
        path_text = args.get("path", "")
        file_path = self._target(path_text)
        content = args.get("content", "")
        if not isinstance(content, str) or len(content.encode("utf-8")) > self._max_bytes:
            raise ToolError("content exceeds size limit")
        if not file_path.parent.is_dir():
            raise ToolError("parent directory does not exist")
        try:
            with os.fdopen(self._open_file(path_text, os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                           "wb") as handle:
                handle.write(content.encode("utf-8"))
        except FileExistsError as exc:
            raise ToolError("refusing to overwrite") from exc
        return f"Created {file_path.name} ({len(content.encode('utf-8'))} bytes)"

    def read_text(self, args: Mapping[str, str]) -> str:
        path_text = args.get("path", "")
        try:
            with os.fdopen(self._open_file(path_text, os.O_RDONLY), "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
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
