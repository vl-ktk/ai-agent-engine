"""Stage 6.13: pin and recheck an exact, task-scoped Git source identity.

Read-only Git inspection only. The immutable receipt contains the original
trusted commit, the resulting commit, and a SHA-256 identity for *uncommitted*
tracked diff plus nonignored untracked file bytes. It is not a Git push, a
Git patch-id, or a claim that uncommitted bytes are in remote source control.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import PurePosixPath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time

from .task_api import OwnerScope, TaskInbox
from .task_assignment import InvalidAssignment, WorkspaceBusy
from .task_gpt import GPTTaskExecutor, GPTTaskOutcome
from .task_response import InvalidTaskResponse, adapt_task_outcome
from .task_state import TaskState
from .task_storage import StorageUnavailable


_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PATCH = re.compile(r"[0-9a-f]{64}\Z")
MAX_GIT_OUTPUT = 8_388_608
MAX_UNTRACKED_BYTES = 8_388_608
MAX_UNTRACKED_FILES = 4096
MAX_GIT_SECONDS = 15


class SourceVersionRejected(RuntimeError):
    """A stable refusal code. Never includes paths, source or Git stderr."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SourceVersionReceipt:
    task_id: str
    worker_id: str
    baseline_commit: str
    result_commit: str
    patch_sha256: str | None
    recorded_at: str


@dataclass(frozen=True, slots=True)
class _Snapshot:
    commit: str
    patch: str | None


def _git(workspace: str, arguments: tuple[str, ...]) -> bytes:
    """Use only fixed trusted read-only argv with bounded output and deadline."""
    binary = shutil.which("git", path="/usr/bin:/bin")
    if not binary:
        raise SourceVersionRejected("git_unavailable")
    argv = (binary, "--no-pager", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null", "-c", "diff.external=",
            "-c", "submodule.recurse=false", *arguments)
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1"}
    child = None
    try:
        child = subprocess.Popen(argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        output = bytearray()
        deadline = time.monotonic() + MAX_GIT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceVersionRejected("git_unavailable")
                for key, _mask in selector.select(min(remaining, 0.2)):
                    piece = os.read(key.fileobj.fileno(),
                                    min(65536, MAX_GIT_OUTPUT - len(output) + 1))
                    if not piece:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(piece)
                    if len(output) > MAX_GIT_OUTPUT:
                        raise SourceVersionRejected("source_too_large")
        if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise SourceVersionRejected("git_unavailable")
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired):
        raise SourceVersionRejected("git_unavailable") from None
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait()
            if child.stdout is not None:
                child.stdout.close()


def _open_untracked(root_fd: int, raw_path: bytes) -> bytes:
    """Anchor an untracked file to a pinned root; reject links and path swaps."""
    try:
        path = raw_path.decode("utf-8", errors="strict")
    except UnicodeError:
        raise SourceVersionRejected("unsafe_source") from None
    if (not path or len(raw_path) > 512 or "\\" in path or "\x00" in path
            or ":" in path or path.startswith("/")
            or any(p in ("", ".", "..") for p in path.split("/"))
            or PurePosixPath(path).is_absolute()):
        raise SourceVersionRejected("unsafe_source")
    folder = os.dup(root_fd)
    fd = None
    try:
        root = os.fstat(folder)
        if not stat.S_ISDIR(root.st_mode):
            raise SourceVersionRejected("unsafe_source")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        for part in path.split("/")[:-1]:
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=folder)
            try:
                info = os.fstat(nxt)
                if (not stat.S_ISDIR(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_dev != root.st_dev
                        or info.st_mode & 0o022):
                    raise SourceVersionRejected("unsafe_source")
            except BaseException:
                os.close(nxt)
                raise
            os.close(folder)
            folder = nxt
        fd = os.open(path.split("/")[-1], flags | os.O_NONBLOCK, dir_fd=folder)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_dev != root.st_dev or before.st_nlink != 1
                or before.st_mode & 0o022):
            raise SourceVersionRejected("unsafe_source")
        if before.st_size > MAX_UNTRACKED_BYTES:
            raise SourceVersionRejected("source_too_large")
        content = bytearray()
        while True:
            block = os.read(fd, min(65536, MAX_UNTRACKED_BYTES - len(content) + 1))
            if not block:
                break
            content.extend(block)
            if len(content) > MAX_UNTRACKED_BYTES:
                raise SourceVersionRejected("source_too_large")
        after = os.fstat(fd)
        attrs = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_ctime_ns, s.st_mtime_ns)
        if attrs(before) != attrs(after) or len(content) != before.st_size:
            raise SourceVersionRejected("source_changed")
        return bytes(content)
    except OSError:
        raise SourceVersionRejected("unsafe_source") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(folder)


def _snapshot(workspace: str, root_fd: int) -> _Snapshot:
    """Compute exact HEAD and a non-replayable digest for dirty tree contents."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise SourceVersionRejected("secure_io_unavailable")
    try:
        gitdir = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(gitdir.st_mode) or gitdir.st_uid != os.geteuid():
            raise SourceVersionRejected("unsafe_repository")
    except OSError:
        raise SourceVersionRejected("unsafe_repository") from None
    top = _git(workspace, ("rev-parse", "--show-toplevel")).strip()
    if top != os.fsencode(workspace):
        raise SourceVersionRejected("unsafe_repository")
    head = _git(workspace, ("rev-parse", "--verify", "HEAD^{commit}")).strip()
    try:
        commit = head.decode("ascii")
    except UnicodeError:
        raise SourceVersionRejected("unsafe_repository") from None
    if not _COMMIT.fullmatch(commit):
        raise SourceVersionRejected("unsafe_repository")
    # Reject unmerged index and submodules, whose dirty content is NOT
    # representable by a superproject diff alone.
    tracked = _git(workspace, ("ls-files", "--stage", "-z"))
    for raw in tracked.split(b"\x00"):
        if not raw:
            continue
        meta, separator, _path = raw.partition(b"\t")
        fields = meta.split()
        if (not separator or len(fields) != 3 or fields[2] != b"0"
                or fields[0] in (b"160000", b"120000")):
            raise SourceVersionRejected("unsupported_repository")
    delta = _git(workspace, ("diff", "--binary", "--no-ext-diff",
                             "--no-textconv", "HEAD", "--"))
    raw_untracked = _git(workspace, ("ls-files", "--others",
                                    "--exclude-standard", "-z"))
    files = sorted(p for p in raw_untracked.split(b"\x00") if p)
    if len(files) > MAX_UNTRACKED_FILES:
        raise SourceVersionRejected("source_too_large")
    if not delta and not files:
        return _Snapshot(commit, None)
    digest = sha256(b"AE-SOURCE-PATCH-DIGEST-v1\x00")
    digest.update(len(delta).to_bytes(8, "big"))
    digest.update(delta)
    total = 0
    for path in files:
        data = _open_untracked(root_fd, path)
        total += len(data)
        if total > MAX_UNTRACKED_BYTES:
            raise SourceVersionRejected("source_too_large")
        digest.update(len(path).to_bytes(4, "big"))
        digest.update(path)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return _Snapshot(commit, digest.hexdigest())


class TaskSourceVersionRecorder:
    """Trusted post-task Git identity receipt with repeated source rechecks."""

    def __init__(self, inbox: TaskInbox, scope: OwnerScope) -> None:
        if (not isinstance(inbox, TaskInbox) or not inbox.durable
                or not isinstance(scope, OwnerScope)
                or inbox._workspace_root is None):
            raise SourceVersionRejected("trusted_context_required")
        self.inbox = inbox
        self.scope = scope

    def _workspace(self, task_id: str):
        with self.inbox._lock:
            self.inbox._scoped_record(task_id, self.scope)
            assignment = self.inbox._assignments.get(task_id)
            if assignment is None or self.inbox._states[task_id] is not TaskState.SUCCEEDED:
                raise SourceVersionRejected("task_not_complete")
        try:
            lease = self.inbox._workspace_root.acquire(assignment.workspace_name)
        except WorkspaceBusy:
            raise SourceVersionRejected("workspace_busy") from None
        except InvalidAssignment:
            raise SourceVersionRejected("unsafe_repository") from None
        if lease.directory != assignment.workspace_dir or lease._fd is None:
            lease.close()
            raise SourceVersionRejected("unsafe_repository")
        return assignment, lease

    def record(self, runner: GPTTaskExecutor, outcome: GPTTaskOutcome,
               *, baseline_commit: str) -> SourceVersionReceipt:
        """Pin baseline, HEAD and dirty-tree digest after a completed provider ACK.

        A baseline is captured by the trusted coordinator *before* execution.
        No Git writes/commits/pushes happen in this method.
        """
        if (not isinstance(runner, GPTTaskExecutor)
                or runner.inbox is not self.inbox or runner.scope != self.scope
                or not isinstance(outcome, GPTTaskOutcome)
                or type(baseline_commit) is not str
                or not _COMMIT.fullmatch(baseline_commit)):
            raise SourceVersionRejected("invalid_source")
        try:
            result = adapt_task_outcome(runner, outcome)
        except InvalidTaskResponse:
            raise SourceVersionRejected("invalid_source") from None
        if result.state is not TaskState.SUCCEEDED:
            raise SourceVersionRejected("task_not_complete")
        ack = runner.delivery.snapshot(result.task_id, self.scope)
        if ack is None or (ack.status, ack.result) != ("completed", "success"):
            raise SourceVersionRejected("task_not_complete")
        assignment, lease = self._workspace(result.task_id)
        try:
            if assignment.worker_id != runner.worker_id:
                raise SourceVersionRejected("invalid_source")
            with self.inbox._lock:
                if self.inbox._store.read_source_version(result.task_id) is not None:
                    raise SourceVersionRejected("already_recorded")
            first = _snapshot(lease.directory, lease._fd)
            # Confirm baseline is an actual ancestor of current HEAD, without
            # trusting task text or accepting a force-rewritten branch.
            try:
                _git(lease.directory, ("cat-file", "-e", baseline_commit + "^{commit}"))
                _git(lease.directory, ("merge-base", "--is-ancestor",
                                       baseline_commit, first.commit))
            except SourceVersionRejected:
                raise SourceVersionRejected("source_diverged") from None
            second = _snapshot(lease.directory, lease._fd)
            if first != second:
                raise SourceVersionRejected("source_changed")
            try:
                with self.inbox._lock:
                    self.inbox._scoped_record(result.task_id, self.scope)
                    if (self.inbox._states[result.task_id] is not TaskState.SUCCEEDED
                            or self.inbox._assignments.get(result.task_id) != assignment):
                        raise SourceVersionRejected("invalid_source")
                    self.inbox._store.write_source_version(
                        result.task_id, runner.worker_id, baseline_commit,
                        first.commit, first.patch)
                    row = self.inbox._store.read_source_version(result.task_id)
            except StorageUnavailable:
                raise SourceVersionRejected("storage_unavailable") from None
            third = _snapshot(lease.directory, lease._fd)
            if third != first:
                raise SourceVersionRejected("source_changed")
            return self._receipt(result.task_id, row)
        finally:
            lease.close()

    @staticmethod
    def _receipt(task_id: str, row: tuple | None) -> SourceVersionReceipt:
        if row is None:
            raise SourceVersionRejected("not_found")
        worker, base, commit, patch, at = row
        if (type(worker) is not str or type(base) is not str
                or not _COMMIT.fullmatch(base) or type(commit) is not str
                or not _COMMIT.fullmatch(commit)
                or (patch is not None and (type(patch) is not str
                                           or not _PATCH.fullmatch(patch)))
                or type(at) is not str):
            raise SourceVersionRejected("version_corrupt")
        return SourceVersionReceipt(task_id, worker, base, commit, patch, at)

    def read_version(self, task_id: str) -> SourceVersionReceipt:
        """Owner-scoped durable metadata read, not a fresh Git verification."""
        with self.inbox._lock:
            self.inbox._scoped_record(task_id, self.scope)
            try:
                stored = self.inbox._store.read_source_version(task_id)
            except StorageUnavailable:
                raise SourceVersionRejected("storage_unavailable") from None
        return self._receipt(task_id, stored)

    def verify_version(self, task_id: str) -> SourceVersionReceipt:
        """Reacquire the exact workspace and confirm it still matches receipt."""
        saved = self.read_version(task_id)
        assignment, lease = self._workspace(task_id)
        try:
            if assignment.worker_id != saved.worker_id:
                raise SourceVersionRejected("version_corrupt")
            fresh = _snapshot(lease.directory, lease._fd)
            if (fresh.commit, fresh.patch) != (
                    saved.result_commit, saved.patch_sha256):
                raise SourceVersionRejected("source_changed")
            return saved
        finally:
            lease.close()
