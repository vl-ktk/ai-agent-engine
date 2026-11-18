"""6.12: trusted, atomic report and workspace-artifact transfer to core.

An executor's completed, owner-scoped outcome is the only admission authority.
Manifest paths are never interpreted by a shell or used outside a pinned
workspace directory. The core stores actual verified bytes in private SQLite.
No public endpoint, provider API call or delegated model permission is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
import stat

from .task_api import OwnerScope, TaskInbox
from .task_assignment import InvalidAssignment, WorkspaceBusy
from .task_gpt import GPTTaskExecutor, GPTTaskOutcome
from .task_response import (
    ArtifactDescriptor, InvalidTaskResponse, MAX_ARTIFACTS,
    TaskExecutionResponse, adapt_task_outcome, parse_task_response,
)
from .task_state import TaskState
from .task_storage import StorageUnavailable


MAX_TRANSFER_FILE_BYTES = 2_097_152
MAX_TRANSFER_TOTAL_BYTES = 8_388_608


class ArtifactTransferRejected(RuntimeError):
    """A stable, sanitized refusal code, never a private path or file body."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TransferredBundle:
    """Verified report plus core-owned copies of the exact artifact bytes."""

    response: TaskExecutionResponse
    files: tuple[tuple[str, bytes], ...]

    def read_file(self, path: str) -> bytes:
        for name, data in self.files:
            if name == path:
                return data
        raise ArtifactTransferRejected("not_found")


def _open_pinned_file(root_fd: int, path: str) -> int:
    """Walk every component with openat/no-follow, never string path resolution."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ArtifactTransferRejected("secure_io_unavailable")
    folder_fd = os.dup(root_fd)
    try:
        root_info = os.fstat(folder_fd)
        if (not stat.S_ISDIR(root_info.st_mode)
                or stat.S_IMODE(root_info.st_mode) != 0o700
                or root_info.st_uid != os.geteuid()):
            raise ArtifactTransferRejected("unsafe_path")
        parts = path.split("/")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        for part in parts[:-1]:
            next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=folder_fd)
            try:
                info = os.fstat(next_fd)
                if (not stat.S_ISDIR(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_dev != root_info.st_dev
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ArtifactTransferRejected("unsafe_path")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(folder_fd)
            folder_fd = next_fd
        return os.open(parts[-1], flags | getattr(os, "O_NONBLOCK", 0),
                       dir_fd=folder_fd)
    except OSError:
        raise ArtifactTransferRejected("unsafe_path") from None
    finally:
        os.close(folder_fd)


def _read_verified(root_fd: int, artifact: ArtifactDescriptor) -> bytes:
    """Bounded read of a pinned regular file with exact digest and size."""
    fd = _open_pinned_file(root_fd, artifact.path)
    try:
        before = os.fstat(fd)
        root_info = os.fstat(root_fd)
        if (not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_dev != root_info.st_dev
                or before.st_nlink != 1):
            raise ArtifactTransferRejected("unsafe_file")
        if (before.st_size > MAX_TRANSFER_FILE_BYTES
                or artifact.size_bytes > MAX_TRANSFER_FILE_BYTES):
            raise ArtifactTransferRejected("size_limit")
        with os.fdopen(os.dup(fd), "rb") as handle:
            content = handle.read(MAX_TRANSFER_FILE_BYTES + 1)
        after = os.fstat(fd)
        attrs = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if attrs(before) != attrs(after):
            raise ArtifactTransferRejected("file_changed")
        if len(content) > MAX_TRANSFER_FILE_BYTES:
            raise ArtifactTransferRejected("size_limit")
        if len(content) != artifact.size_bytes:
            raise ArtifactTransferRejected("size_mismatch")
        if sha256(content).hexdigest() != artifact.sha256:
            raise ArtifactTransferRejected("checksum_mismatch")
        return content
    except OSError:
        raise ArtifactTransferRejected("unsafe_file") from None
    finally:
        os.close(fd)


class TaskArtifactTransfer:
    """Trusted post-execution bridge from a task workspace to private core DB.

    Ingest occurs after successful provider ACK, and temporarily reacquires
    the *same* workspace's directory flock. If a new task already owns it,
    ingestion refuses rather than copying its files. The original worker's
    manifest cannot choose owner, project, worker or workspace.
    """

    def __init__(self, inbox: TaskInbox, scope: OwnerScope) -> None:
        if (not isinstance(inbox, TaskInbox) or not inbox.durable
                or not isinstance(scope, OwnerScope)
                or inbox._workspace_root is None):
            raise ArtifactTransferRejected("trusted_context_required")
        self.inbox = inbox
        self.scope = scope

    def ingest(self, runner: GPTTaskExecutor, outcome: GPTTaskOutcome,
               manifest: tuple[ArtifactDescriptor, ...] = ()) -> TransferredBundle:
        """Verify provider receipt, copy bytes, persist one immutable bundle."""
        if (not isinstance(runner, GPTTaskExecutor)
                or runner.inbox is not self.inbox or runner.scope != self.scope
                or not isinstance(outcome, GPTTaskOutcome)
                or type(manifest) is not tuple or len(manifest) > MAX_ARTIFACTS
                or any(not isinstance(a, ArtifactDescriptor) for a in manifest)):
            raise ArtifactTransferRejected("invalid_source")
        try:
            original = adapt_task_outcome(runner, outcome)
            if original.state is not TaskState.SUCCEEDED:
                raise ArtifactTransferRejected("task_not_complete")
            response = TaskExecutionResponse(
                original.task_id, original.provider, original.model_id,
                original.state, original.text, manifest, None,
                original.timeout_ms, original.text_chars)
            validated = parse_task_response(response.to_bytes())
        except InvalidTaskResponse:
            raise ArtifactTransferRejected("invalid_manifest") from None
        except (ValueError, UnicodeError, TypeError):
            raise ArtifactTransferRejected("invalid_manifest") from None
        task_id = validated.task_id
        assignment = self.inbox.get_assignment(task_id, self.scope)
        if assignment is None or assignment.worker_id != runner.worker_id:
            raise ArtifactTransferRejected("invalid_source")
        ack = runner.delivery.snapshot(task_id, self.scope)
        if ack is None or ack.status != "completed" or ack.result != "success":
            raise ArtifactTransferRejected("task_not_complete")
        if self.inbox.state(task_id, self.scope) is not TaskState.SUCCEEDED:
            raise ArtifactTransferRejected("task_not_complete")
        try:
            lease = self.inbox._workspace_root.acquire(assignment.workspace_name)
        except WorkspaceBusy:
            raise ArtifactTransferRejected("workspace_busy") from None
        except InvalidAssignment:
            raise ArtifactTransferRejected("unsafe_path") from None
        try:
            if lease.directory != assignment.workspace_dir or lease._fd is None:
                raise ArtifactTransferRejected("unsafe_path")
            with self.inbox._lock:
                self.inbox._scoped_record(task_id, self.scope)
                if (self.inbox._states[task_id] is not TaskState.SUCCEEDED
                        or self.inbox._assignments.get(task_id) != assignment):
                    raise ArtifactTransferRejected("invalid_source")
                if self.inbox._store.read_artifact_bundle(task_id) is not None:
                    raise ArtifactTransferRejected("already_transferred")
            payloads: list[tuple[str, str, int, str, bytes]] = []
            total = 0
            for artifact in validated.artifacts:
                if artifact.size_bytes > MAX_TRANSFER_FILE_BYTES:
                    raise ArtifactTransferRejected("size_limit")
                total += artifact.size_bytes
                if total > MAX_TRANSFER_TOTAL_BYTES:
                    raise ArtifactTransferRejected("size_limit")
                data = _read_verified(lease._fd, artifact)
                payloads.append((artifact.path, artifact.sha256,
                                 artifact.size_bytes, artifact.media_type, data))
            try:
                with self.inbox._lock:
                    self.inbox._scoped_record(task_id, self.scope)
                    if (self.inbox._states[task_id] is not TaskState.SUCCEEDED
                            or self.inbox._assignments.get(task_id) != assignment):
                        raise ArtifactTransferRejected("invalid_source")
                    self.inbox._store.write_artifact_bundle(
                        task_id, validated.to_bytes(), tuple(payloads))
            except StorageUnavailable:
                raise ArtifactTransferRejected("storage_unavailable") from None
            return TransferredBundle(
                validated, tuple(sorted((p, data) for p, _, _, _, data in payloads)))
        finally:
            lease.close()

    def read_bundle(self, task_id: str) -> TransferredBundle:
        """Scoped read back, re-verifying the stored byte digests on every read."""
        with self.inbox._lock:
            self.inbox._scoped_record(task_id, self.scope)
            try:
                stored = self.inbox._store.read_artifact_bundle(task_id)
            except StorageUnavailable:
                raise ArtifactTransferRejected("artifact_corrupt") from None
        if stored is None:
            raise ArtifactTransferRejected("not_found")
        report_bytes, rows = stored
        try:
            report = parse_task_response(report_bytes)
            if report.task_id != task_id or report.state is not TaskState.SUCCEEDED:
                raise ArtifactTransferRejected("artifact_corrupt")
            expected = {a.path: a for a in report.artifacts}
            if len(expected) != len(rows):
                raise ArtifactTransferRejected("artifact_corrupt")
            contents = []
            for path, digest, size, mime, body in rows:
                descriptor = expected.get(path)
                if (descriptor is None or descriptor.sha256 != digest
                        or descriptor.size_bytes != size or descriptor.media_type != mime
                        or type(body) is not bytes or len(body) != size
                        or sha256(body).hexdigest() != digest):
                    raise ArtifactTransferRejected("artifact_corrupt")
                contents.append((path, body))
            return TransferredBundle(report, tuple(contents))
        except (InvalidTaskResponse, ValueError, TypeError):
            raise ArtifactTransferRejected("artifact_corrupt") from None
