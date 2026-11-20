"""Trusted server-side client for the separate Netcup Secure Vault service.

The caller's Linux UID, rather than any JSON field or model assertion,
determines Vault permissions. The main orchestrator can save newly supplied
credentials only into categories already granted to its ae-runtime identity.
This module is NOT a ChatGPT transport, an authenticator, or a model tool.

Never pass secret values through CLI flags, process environments, logger,
GitHub or task prompts. The caller already holds the bytes in memory.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
from typing import Any

from .secure_vault import VaultDenied, VaultUnavailable


_SOCKET_PATH = Path("/run/ae-vault.sock")
_MAX_REQUEST = 100_000
_MAX_RESPONSE = 140_000
_MAX_SECRET_BYTES = 65_536


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VaultUnavailable()
        result[key] = value
    return result


class VaultUnixClient:
    """Mutually host-scoped AF_UNIX IPC, no remote listener or password log.

    The production constructor intentionally pins the socket path and Linux
    peer account. An explicit custom path+UID is for disposable tests only;
    untrusted input must never be allowed to supply either of them.
    """

    def __init__(self, *, socket_path: Path = _SOCKET_PATH,
                 expected_service_uid: int | None = None) -> None:
        if (not isinstance(socket_path, Path) or not socket_path.is_absolute()
                or socket_path.is_symlink()):
            raise VaultUnavailable()
        if expected_service_uid is None:
            try:
                expected_service_uid = pwd.getpwnam("ae-vault").pw_uid
            except KeyError:
                raise VaultUnavailable() from None
        if type(expected_service_uid) is not int or expected_service_uid < 0:
            raise VaultUnavailable()
        self._socket_path = socket_path
        self._expected_uid = expected_service_uid

    def _send(self, operation: str, **fields: object) -> dict[str, Any]:
        try:
            request = json.dumps({"operation": operation, **fields},
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
            raise VaultDenied() from None
        if len(request) > _MAX_REQUEST:
            raise VaultDenied()
        try:
            meta = os.lstat(self._socket_path)
            if (not stat.S_ISSOCK(meta.st_mode)
                    or meta.st_uid != self._expected_uid
                    or stat.S_IMODE(meta.st_mode) != 0o660):
                raise VaultUnavailable()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(5)
                conn.connect(str(self._socket_path))
                peer_info = conn.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", peer_info)
                if uid != self._expected_uid:
                    raise VaultUnavailable()
                conn.sendall(request)
                reply = bytearray()
                while len(reply) <= _MAX_RESPONSE:
                    chunk = conn.recv(min(8192, _MAX_RESPONSE + 1 - len(reply)))
                    if not chunk:
                        break
                    reply.extend(chunk)
                    if b"\n" in chunk:
                        break
            if len(reply) > _MAX_RESPONSE or not reply.endswith(b"\n"):
                raise VaultUnavailable()
            response = json.loads(reply[:-1].decode("utf-8"),
                                  object_pairs_hook=_unique_pairs)
            if type(response) is not dict or type(response.get("ok")) is not bool:
                raise VaultUnavailable()
            if not response["ok"]:
                raise VaultDenied()
            return response
        except VaultDenied:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError,
                OverflowError, VaultUnavailable):
            raise VaultUnavailable() from None

    def status(self) -> bool:
        response = self._send("status")
        if response != {"ok": True, "status": "ready"}:
            raise VaultUnavailable()
        return True

    def save_secret(self, *, category_id: str, label: str,
                    kind: str, value: bytes) -> str:
        if type(value) is not bytes or not 0 < len(value) <= _MAX_SECRET_BYTES:
            raise VaultDenied()
        response = self._send("save", category_id=category_id, label=label,
                              kind=kind, value_b64=base64.b64encode(value).decode("ascii"))
        secret_id = response.get("secret_id")
        if type(secret_id) is not str or not secret_id.startswith("sec_"):
            raise VaultUnavailable()
        return secret_id

    def get_secret(self, secret_id: str) -> bytes:
        response = self._send("get", secret_id=secret_id)
        encoded = response.get("value_b64")
        if type(encoded) is not str or len(encoded) > 90_000:
            raise VaultUnavailable()
        try:
            value = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise VaultUnavailable() from None
        if not 0 < len(value) <= _MAX_SECRET_BYTES:
            raise VaultUnavailable()
        return value

    def update_secret(self, secret_id: str, value: bytes) -> int:
        if type(value) is not bytes or not 0 < len(value) <= _MAX_SECRET_BYTES:
            raise VaultDenied()
        response = self._send("update", secret_id=secret_id,
                              value_b64=base64.b64encode(value).decode("ascii"))
        revision = response.get("revision")
        if type(revision) is not int or revision < 1:
            raise VaultUnavailable()
        return revision

    def list_metadata(self) -> tuple[dict[str, object], ...]:
        response = self._send("list")
        entries = response.get("secrets")
        if type(entries) is not list or len(entries) > 1000:
            raise VaultUnavailable()
        result: list[dict[str, object]] = []
        for entry in entries:
            if (type(entry) is not dict or set(entry) !=
                    {"id", "label", "kind", "revision"}
                    or type(entry["id"]) is not str
                    or type(entry["label"]) is not str
                    or type(entry["kind"]) is not str
                    or type(entry["revision"]) is not int):
                raise VaultUnavailable()
            result.append(entry)
        return tuple(result)


class VaultOrchestratorTasks:
    """Operations for an *already authenticated* owner-chat task dispatcher.

    No extra confirmation when the owner deliberately sends a password, and
    no automatic password-rotation warning. The Linux Vault service remains
    the authority on category/project permission: no model-granted scope.
    """

    def __init__(self, client: VaultUnixClient) -> None:
        if not isinstance(client, VaultUnixClient):
            raise VaultUnavailable()
        self._client = client

    def save_supplied_secret(self, *, category_id: str, label: str,
                             kind: str, value: bytes) -> str:
        return self._client.save_secret(category_id=category_id, label=label,
                                        kind=kind, value=value)

    def use_approved_secret(self, secret_id: str, consume) -> bool:
        if not callable(consume):
            raise VaultDenied()
        value = self._client.get_secret(secret_id)
        result = consume(value)
        if type(result) is not bool:
            raise VaultDenied()
        return result
