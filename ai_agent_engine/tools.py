"""Constrained filesystem tools for an isolated test directory only."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
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
        if not isinstance(path_text, str) or not path_text:
            raise ToolError("invalid path")
        path = PurePosixPath(path_text)
        if path.is_absolute() or any(part in (".", "..") for part in path_text.split("/")):
            raise ToolError("path escapes sandbox")
        resolved = (self._root / path_text).resolve(strict=False)
        if not resolved.is_relative_to(self._root) or resolved == self._root:
            raise ToolError("path escapes sandbox")
        return resolved

    def write_new(self, args: Mapping[str, str]) -> str:
        file_path = self._target(args.get("path", ""))
        content = args.get("content", "")
        if not isinstance(content, str) or len(content.encode("utf-8")) > self._max_bytes:
            raise ToolError("content exceeds size limit")
        if not file_path.parent.is_dir():
            raise ToolError("parent directory does not exist")
        try:
            with file_path.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise ToolError("refusing to overwrite") from exc
        return f"Created {file_path.name} ({len(content.encode('utf-8'))} bytes)"

    def read_text(self, args: Mapping[str, str]) -> str:
        file_path = self._target(args.get("path", ""))
        if not file_path.is_file() or file_path.stat().st_size > self._max_bytes:
            raise ToolError("file unavailable or too large")
        return file_path.read_text(encoding="utf-8")

    def allowed_tools(self) -> tuple[Tool, ...]:
        return (Tool("write_new_file", self.write_new), Tool("read_file", self.read_text))
