"""Stage 6.5: strict, provider-neutral terminal task response contract.

The adapter consumes trusted in-memory GPT/Claude outcomes and the private
provider audit. It never reads arbitrary output from an untrusted subprocess,
grants permissions, persists model text or claims file transfer (stage 6.12).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .task_api import _TASK_ID
from .task_gpt import GPTTaskExecutor, GPTTaskOutcome, _MODEL
from .task_state import TaskState


RESPONSE_VERSION = 1
MAX_RESPONSE_BYTES = 131_072  # 20k Unicode characters plus envelope metadata
MAX_TEXT_CHARS = 20_000
MAX_ARTIFACTS = 16
MAX_ARTIFACT_SIZE = 100_000_000  # descriptor only, no file transfer
_PROVIDERS = frozenset({"gpt", "claude"})
_ERROR_CODES = frozenset({"quota", "auth", "unsupported", "unavailable",
                          "worker_failure", "invalid_result", "timed_out", "cancelled"})
_FAILURE_CODES = _ERROR_CODES - {"timed_out", "cancelled"}
_TERMINAL = frozenset({TaskState.SUCCEEDED, TaskState.FAILED,
                        TaskState.TIMED_OUT, TaskState.CANCELLED})
_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}){0,7}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MIME = re.compile(r"[a-z][a-z0-9.+-]{0,49}/[a-z0-9][a-z0-9.+-]{0,49}\Z")


class InvalidTaskResponse(ValueError):
    """Sanitized response rejection, with no leaked provider result."""


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    path: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class TaskExecutionResponse:
    task_id: str
    provider: str
    model_id: str
    state: TaskState
    text: str | None
    artifacts: tuple[ArtifactDescriptor, ...]
    error_code: str | None
    timeout_ms: int
    text_chars: int

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": RESPONSE_VERSION,
            "task_id": self.task_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "state": self.state.value,
            "result": ({"text": self.text,
                        "artifacts": [{"path": a.path, "sha256": a.sha256,
                                       "size_bytes": a.size_bytes,
                                       "media_type": a.media_type}
                                      for a in self.artifacts]}
                       if self.state is TaskState.SUCCEEDED else None),
            "error": ({"code": self.error_code}
                      if self.error_code is not None else None),
            "limits": {
                "timeout_ms": self.timeout_ms,
                "max_text_chars": MAX_TEXT_CHARS,
                "text_chars": self.text_chars,
                "token_usage": None,
            },
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_payload(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidTaskResponse("invalid task response")
        result[key] = value
    return result


def _reject_constant(_name: str) -> None:
    raise InvalidTaskResponse("invalid task response")


def _shape(value: object, keys: frozenset[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise InvalidTaskResponse("invalid task response")
    return value


def _text(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if (type(value) is not str or len(value) > MAX_TEXT_CHARS
            or not value.strip() or "\x00" in value):
        raise InvalidTaskResponse("invalid task response")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise InvalidTaskResponse("invalid task response") from None
    return value


def parse_task_response(raw: bytes) -> TaskExecutionResponse:
    """Validate exact response structure and cross-field invariants.

    Parsing does not prove that any file exists or any usage figure is real.
    """
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise InvalidTaskResponse("invalid task response")
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"),
                             object_pairs_hook=_pairs,
                             parse_constant=_reject_constant)
        root = _shape(payload, frozenset({"schema_version", "task_id",
                                         "provider", "model_id", "state",
                                         "result", "error", "limits"}))
        if type(root["schema_version"]) is not int or root["schema_version"] != RESPONSE_VERSION:
            raise InvalidTaskResponse("invalid task response")
        tid, provider, model = root["task_id"], root["provider"], root["model_id"]
        if (type(tid) is not str or not _TASK_ID.fullmatch(tid)
                or type(provider) is not str or provider not in _PROVIDERS
                or type(model) is not str or not _MODEL.fullmatch(model)):
            raise InvalidTaskResponse("invalid task response")
        if type(root["state"]) is not str:
            raise InvalidTaskResponse("invalid task response")
        state = TaskState(root["state"])
        if state not in _TERMINAL:
            raise InvalidTaskResponse("invalid task response")

        limits = _shape(root["limits"], frozenset({"timeout_ms", "max_text_chars",
                                                   "text_chars", "token_usage"}))
        timeout, max_chars, used = (limits["timeout_ms"],
                                    limits["max_text_chars"], limits["text_chars"])
        if (type(timeout) is not int or not 50 <= timeout <= 86_400_000
                or type(max_chars) is not int or max_chars != MAX_TEXT_CHARS
                or type(used) is not int or not 0 <= used <= MAX_TEXT_CHARS
                or limits["token_usage"] is not None):
            raise InvalidTaskResponse("invalid task response")

        if state is TaskState.SUCCEEDED:
            if root["error"] is not None:
                raise InvalidTaskResponse("invalid task response")
            result = _shape(root["result"], frozenset({"text", "artifacts"}))
            text = _text(result["text"], required=False)
            raw_artifacts = result["artifacts"]
            if type(raw_artifacts) is not list or len(raw_artifacts) > MAX_ARTIFACTS:
                raise InvalidTaskResponse("invalid task response")
            artifacts: list[ArtifactDescriptor] = []
            seen_paths: set[str] = set()
            for raw_artifact in raw_artifacts:
                a = _shape(raw_artifact, frozenset({"path", "sha256",
                                                    "size_bytes", "media_type"}))
                path, digest, size, mime = (a["path"], a["sha256"],
                                            a["size_bytes"], a["media_type"])
                if (type(path) is not str or not _PATH.fullmatch(path)
                        or any(part in (".", "..") for part in path.split("/"))
                        or path in seen_paths or type(digest) is not str
                        or not _SHA256.fullmatch(digest)
                        or type(size) is not int or not 0 <= size <= MAX_ARTIFACT_SIZE
                        or type(mime) is not str or not _MIME.fullmatch(mime)):
                    raise InvalidTaskResponse("invalid task response")
                seen_paths.add(path)
                artifacts.append(ArtifactDescriptor(path, digest, size, mime))
            if text is None and not artifacts:
                raise InvalidTaskResponse("invalid task response")
            if used != (len(text) if text is not None else 0):
                raise InvalidTaskResponse("invalid task response")
            error_code = None
        else:
            if root["result"] is not None or used != 0:
                raise InvalidTaskResponse("invalid task response")
            error = _shape(root["error"], frozenset({"code"}))
            code = error["code"]
            if (type(code) is not str or code not in _ERROR_CODES
                    or (state is TaskState.FAILED and code not in _FAILURE_CODES)
                    or (state is TaskState.CANCELLED and code != "cancelled")
                    or (state is TaskState.TIMED_OUT and code != "timed_out")):
                raise InvalidTaskResponse("invalid task response")
            text, artifacts, error_code = None, [], code
        return TaskExecutionResponse(tid, provider, model, state, text,
                                     tuple(artifacts), error_code, timeout, used)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise InvalidTaskResponse("invalid task response") from None


def adapt_task_outcome(runner: GPTTaskExecutor,
                       outcome: GPTTaskOutcome) -> TaskExecutionResponse:
    """Build a terminal result from a trusted GPT/Claude runner and its audit.

    No artifact is synthesized from model text. Outcome is private caller data;
    a duplicate/no-execution result has no trustworthy text and is refused.
    """
    if (not isinstance(runner, GPTTaskExecutor)
            or not isinstance(outcome, GPTTaskOutcome)
            or outcome.executed is not True
            or outcome.state not in _TERMINAL
            or runner._provider not in _PROVIDERS
            or outcome.model_id != runner.model_id
            or runner.inbox.state(outcome.task_id, runner.scope) is not outcome.state):
        raise InvalidTaskResponse("unverified task execution")
    history = runner.provider_history(outcome.task_id)
    if (len(history) != 1
            or history[0].task_id != outcome.task_id
            or history[0].provider != runner._provider
            or history[0].model_id != runner.model_id):
        raise InvalidTaskResponse("unverified provider audit")
    code = history[0].outcome
    if outcome.state is TaskState.SUCCEEDED:
        if code != "completed" or type(outcome.text) is not str or not outcome.text.strip():
            raise InvalidTaskResponse("unverified success")
        result = {"text": outcome.text, "artifacts": []}
        error = None
    else:
        if outcome.text is not None or (
                (outcome.state is TaskState.FAILED and code not in _FAILURE_CODES)
                or (outcome.state is TaskState.CANCELLED and code != "cancelled")
                or (outcome.state is TaskState.TIMED_OUT and code != "timed_out")):
            raise InvalidTaskResponse("unverified failure")
        result = None
        error = {"code": code}
    payload = {
        "schema_version": RESPONSE_VERSION,
        "task_id": outcome.task_id, "provider": runner._provider,
        "model_id": runner.model_id, "state": outcome.state.value,
        "result": result, "error": error,
        "limits": {
            "timeout_ms": round(runner.timeout_seconds * 1000),
            "max_text_chars": MAX_TEXT_CHARS,
            "text_chars": len(outcome.text) if outcome.text is not None else 0,
            "token_usage": None,
        },
    }
    try:
        raw = json.dumps(payload, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeError):
        raise InvalidTaskResponse("unverified task execution") from None
    return parse_task_response(raw)
