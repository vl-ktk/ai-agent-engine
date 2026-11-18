"""Stage 6.7: task-bound, exact-command shell/Git/compile/test allowlist.

No shell interpolation, model argv, Git network commands, arbitrary script
execution or public command endpoint. Unit tests run ONLY if trusted code
explicitly opted in; 6.8 will add an OS-level process security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from types import MappingProxyType
from typing import Mapping

from .core import Tool
from .task_api import OwnerScope, TaskInbox
from .task_assignment import validate_worker
from .task_permissions import ProtectedAction, TaskPermissionGate


MAX_OUTPUT_BYTES = 4096
_MAX_RUN_SECONDS = 60
_COMPILE_SOURCE = (
    "import pathlib,sys\n"
    "root=pathlib.Path.cwd()\n"
    "count=0\n"
    "for folder in ('ai_agent_engine','tests'):\n"
    "  directory=root/folder\n"
    "  if not directory.is_dir() or directory.is_symlink(): continue\n"
    "  for path in directory.rglob('*.py'):\n"
    "    if path.is_symlink() or not path.is_file(): continue\n"
    "    compile(path.read_bytes(),str(path),'exec',dont_inherit=True)\n"
    "    count+=1\n"
    "print('syntax_pass='+str(count))\n"
)


class CommandDenied(PermissionError):
    """Sanitized refusal; no command string or private path reflected."""


class CommandFailed(RuntimeError):
    """A vetted command failed, exceeded limits or could not start."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    exit_code: int
    output: str


@dataclass(frozen=True, slots=True)
class _CommandSpec:
    argv: tuple[str, ...]
    timeout_seconds: float
    requires_test_opt_in: bool = False


def _trusted_commands() -> dict[str, _CommandSpec]:
    git = shutil.which("git", path="/usr/bin:/bin")
    if not git:
        raise CommandDenied("trusted Git executable unavailable")
    base = (git, "--no-pager", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null")
    return {
        "git_status": _CommandSpec(
            base + ("status", "--short", "--untracked-files=no"), 10),
        "git_diff_check": _CommandSpec(
            base + ("diff", "--no-ext-diff", "--check", "--"), 10),
        # Compiles syntax into RAM only. No source import or .pyc writes.
        "python_compile_check": _CommandSpec(
            (sys.executable, "-I", "-B", "-c", _COMPILE_SOURCE), 15),
        # Test modules can run arbitrary Python and must be preapproved as
        # trusted source; never expose this opt-in through request JSON.
        "python_unit_tests": _CommandSpec(
            (sys.executable, "-B", "-m", "unittest", "discover",
             "-s", "tests", "-q"), 60, requires_test_opt_in=True),
    }


class TaskCommandRunner:
    """One task/worker, one assigned workspace and four immutable command IDs."""

    def __init__(self, inbox: TaskInbox, scope: OwnerScope, *,
                 task_id: str, worker_id: str, gate: TaskPermissionGate,
                 allow_trusted_tests: bool = False) -> None:
        if (not isinstance(inbox, TaskInbox) or not inbox.durable
                or not isinstance(scope, OwnerScope)
                or not isinstance(gate, TaskPermissionGate)
                or gate.inbox is not inbox
                or type(allow_trusted_tests) is not bool):
            raise CommandDenied("trusted task and permission gate required")
        validate_worker(worker_id)
        assigned = inbox.get_assignment(task_id, scope)
        if assigned is None or assigned.worker_id != worker_id:
            raise CommandDenied("task has no matching workspace assignment")
        if os.name != "posix" or not Path("/proc/self/fd").is_dir():
            raise CommandDenied("secure command workspace unavailable")
        self._inbox = inbox
        self._scope = scope
        self._task_id = task_id
        self._worker_id = worker_id
        self._assigned = assigned
        self._gate = gate
        self._allow_tests = allow_trusted_tests
        self._commands = MappingProxyType(_trusted_commands())
        try:
            info = os.lstat(assigned.workspace_dir)
        except OSError:
            raise CommandDenied("assigned workspace unavailable") from None
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise CommandDenied("assigned workspace unavailable")
        self._root_identity = (info.st_dev, info.st_ino)

    @staticmethod
    def _environment(workspace: str) -> dict[str, str]:
        """Minimal, deterministic child environment; never inherit API creds."""
        return {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": workspace, "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat", "PAGER": "cat", "PIP_NO_INDEX": "1",
        }

    def _open_workspace(self) -> int:
        if self._inbox.get_assignment(self._task_id, self._scope) != self._assigned:
            raise CommandDenied("workspace assignment changed")
        try:
            fd = os.open(self._assigned.workspace_dir,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            raise CommandDenied("assigned workspace unavailable") from None
        info = os.fstat(fd)
        if ((info.st_dev, info.st_ino) != self._root_identity
                or not stat.S_ISDIR(info.st_mode)):
            os.close(fd)
            raise CommandDenied("workspace root changed")
        return fd

    @staticmethod
    def _kill_child(child: subprocess.Popen) -> None:
        if child.poll() is None:
            try:
                if os.getpgid(child.pid) == child.pid:
                    os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            raise CommandFailed("trusted command could not stop") from None

    def _execute(self, spec: _CommandSpec, command_id: str) -> CommandResult:
        root_fd = self._open_workspace()
        child = None
        try:
            # /proc/self/fd pins cwd to the checked directory inode, rather
            # than resolving a raceable pathname at subprocess creation.
            cwd = f"/proc/self/fd/{root_fd}"
            child = subprocess.Popen(
                spec.argv, cwd=cwd, pass_fds=(root_fd,),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True,
                env=self._environment(self._assigned.workspace_dir),
                close_fds=True,
            )
            deadline = time.monotonic() + spec.timeout_seconds
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CommandFailed("trusted command timed out")
                    for key, _events in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fileobj.fileno(),
                                        MAX_OUTPUT_BYTES - len(output) + 1)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT_BYTES:
                            raise CommandFailed("trusted command output exceeded limit")
            try:
                exit_code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise CommandFailed("trusted command timed out") from None
            if exit_code != 0:
                raise CommandFailed("trusted command failed")
            result = output.decode("utf-8", errors="replace")
            # Keep printable Git/test facts; no terminal control sequences.
            safe = "".join(c if c.isprintable() or c in "\n\t" else "?" for c in result)
            return CommandResult(command_id, exit_code, safe)
        except (OSError, UnicodeError):
            raise CommandFailed("trusted command unavailable") from None
        finally:
            if child is not None:
                self._kill_child(child)
                if child.stdout is not None:
                    child.stdout.close()
            os.close(root_fd)

    def run(self, command_id: str) -> CommandResult:
        """Only a fixed ID is accepted; no user-provided argv or shell text."""
        if type(command_id) is not str or command_id not in self._commands:
            raise CommandDenied("command not in trusted allowlist")
        spec = self._commands[command_id]
        if spec.requires_test_opt_in and not self._allow_tests:
            raise CommandDenied("test source has not been trusted")

        def execute() -> CommandResult:
            return self._execute(spec, command_id)

        if spec.requires_test_opt_in:
            def write_authorized() -> CommandResult:
                return self._gate.run(
                    self._task_id, self._scope, actor_id=self._worker_id,
                    action=ProtectedAction.WRITE_WORKSPACE, operation=execute,
                )
            operation = write_authorized
        else:
            operation = execute
        return self._gate.run(
            self._task_id, self._scope, actor_id=self._worker_id,
            action=ProtectedAction.EXECUTE_TOOL, operation=operation,
        )

    def allowed_tools(self) -> tuple[Tool, ...]:
        """Expose only the allowlisted command ID, never a shell/argv field."""
        def vetted_tool(args: Mapping[str, str]) -> str:
            result = self.run(args["command_id"])
            return result.output or "trusted command completed"

        return (Tool("run_vetted_command", vetted_tool,
                     required_args=("command_id",),
                     max_output_chars=MAX_OUTPUT_BYTES),)
