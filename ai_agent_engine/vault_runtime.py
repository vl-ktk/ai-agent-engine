"""Netcup-only Secure Vault Unix-socket transport (7.7A).

The Linux kernel, not an untrusted JSON body, determines the calling actor.
Root is the approved owner/admin principal; the designated ae-runtime UID is
the limited engine principal. This is intentionally NOT a public HTTP/MCP
server, owner-chat authenticator, or model-invokable shell tool.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import sqlite3
import stat
import struct
import tempfile
import time
from typing import Any
from uuid import uuid4

from .secure_vault import (
    SecureVault, VaultCaller, VaultDenied, VaultUnavailable,
)


MAX_REQUEST = 100_000
MAX_RESPONSE = 140_000
OWNER_ID = "vl-ktk"
PROJECT_ID = "ai-agent-engine"
WORKER_ACTOR = "main-orchestrator"


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VaultDenied()
        result[key] = value
    return result


def _body(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REQUEST:
        raise VaultDenied()
    try:
        data = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise VaultDenied() from None
    if type(data) is not dict or type(data.get("operation")) is not str:
        raise VaultDenied()
    return data


def _fields(data: dict[str, Any], required: set[str]) -> None:
    if set(data) != required | {"operation"}:
        raise VaultDenied()


def _data_bytes(value: object) -> bytes:
    if type(value) is not str or len(value) > 90_000:
        raise VaultDenied()
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise VaultDenied() from None


def _credential_file(directory: Path, name: str) -> bytes:
    """Read fixed binary credential injected by systemd, never a model path."""
    fd = -1
    try:
        if not directory.is_absolute() or directory.is_symlink():
            raise VaultUnavailable()
        base = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=base)
        finally:
            os.close(base)
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or meta.st_size != 32 or (stat.S_IMODE(meta.st_mode) & 0o007)):
            raise VaultUnavailable()
        data = os.read(fd, 33)
        if len(data) != 32:
            raise VaultUnavailable()
        return data
    except (OSError, ValueError, VaultUnavailable):
        raise VaultUnavailable() from None
    finally:
        if fd >= 0:
            os.close(fd)


class VaultProtocol:
    """In-memory request handler with strict host-bound caller identity."""

    def __init__(self, vault: SecureVault, *, root_uid: int = 0,
                 worker_uid: int) -> None:
        if worker_uid == root_uid or worker_uid < 0:
            raise VaultUnavailable()
        self._vault = vault
        self._root_uid = root_uid
        self._worker_uid = worker_uid

    def _identity(self, uid: int) -> VaultCaller:
        if uid == self._root_uid:
            return VaultCaller(OWNER_ID, PROJECT_ID, "owner-netcup", True)
        if uid == self._worker_uid:
            return VaultCaller(OWNER_ID, PROJECT_ID, WORKER_ACTOR, False)
        raise VaultDenied()

    def process(self, uid: int, raw: bytes) -> bytes:
        """Only direct, verified OS callers; failures never echo submitted data."""
        try:
            caller = self._identity(uid)
            request = _body(raw)
            op = request["operation"]
            if op == "status":
                _fields(request, set())
                result: dict[str, object] = {"ok": True, "status": "ready"}
            elif op == "ensure_project":
                _fields(request, {"project_id"})
                if not caller.is_owner or request["project_id"] != PROJECT_ID:
                    raise VaultDenied()
                root = self._vault.ensure_project(caller, PROJECT_ID)
                result = {"ok": True, "category_id": root}
            elif op == "create_category":
                _fields(request, {"parent_id", "name"})
                result = {"ok": True, "category_id": self._vault.create_category(
                    caller, request["parent_id"], request["name"])}
            elif op == "list_categories":
                _fields(request, set())
                result = {"ok": True, "categories": [
                    {"id": category_id, "project_id": project_id,
                     "parent_id": parent_id, "name": name}
                    for category_id, project_id, parent_id, name
                    in self._vault.list_categories(caller)]}
            elif op == "rename_category":
                _fields(request, {"category_id", "name"})
                self._vault.rename_category(caller, request["category_id"], request["name"])
                result = {"ok": True}
            elif op == "move_category":
                _fields(request, {"category_id", "parent_id"})
                self._vault.move_category(caller, request["category_id"], request["parent_id"])
                result = {"ok": True}
            elif op == "archive_category":
                _fields(request, {"category_id"})
                self._vault.archive_category(caller, request["category_id"])
                result = {"ok": True}
            elif op == "restore_category":
                _fields(request, {"category_id"})
                self._vault.restore_category(caller, request["category_id"])
                result = {"ok": True}
            elif op == "grant_category_save":
                _fields(request, {"category_id", "expires_at"})
                self._vault.grant_category_save(
                    caller, request["category_id"], WORKER_ACTOR,
                    request["expires_at"])
                result = {"ok": True}
            elif op == "revoke_category_save":
                _fields(request, {"category_id"})
                self._vault.revoke_category_save(
                    caller, request["category_id"], WORKER_ACTOR)
                result = {"ok": True}
            elif op == "save":
                _fields(request, {"category_id", "label", "kind", "value_b64"})
                secret_id = self._vault.save_secret(
                    caller, request["category_id"], request["label"],
                    request["kind"], _data_bytes(request["value_b64"]))
                result = {"ok": True, "secret_id": secret_id}
            elif op == "get":
                _fields(request, {"secret_id"})
                value = self._vault.get_secret(caller, request["secret_id"])
                result = {"ok": True,
                          "value_b64": base64.b64encode(value).decode("ascii")}
            elif op == "update":
                _fields(request, {"secret_id", "value_b64"})
                revision = self._vault.update_secret(
                    caller, request["secret_id"], _data_bytes(request["value_b64"]))
                result = {"ok": True, "revision": revision}
            elif op == "move_secret":
                _fields(request, {"secret_id", "category_id"})
                self._vault.move_secret(
                    caller, request["secret_id"], request["category_id"])
                result = {"ok": True}
            elif op == "list":
                _fields(request, set())
                items = self._vault.list_secrets(caller)
                result = {"ok": True, "secrets": [
                    {"id": item.secret_id, "label": item.label,
                     "kind": item.kind, "revision": item.revision}
                    for item in items]}
            elif op == "grant":
                _fields(request, {"secret_id", "operations", "expires_at"})
                operations = request["operations"]
                if type(operations) is not list or any(type(x) is not str for x in operations):
                    raise VaultDenied()
                self._vault.grant(caller, request["secret_id"], WORKER_ACTOR,
                                  PROJECT_ID, frozenset(operations),
                                  request["expires_at"])
                result = {"ok": True}
            elif op == "revoke":
                _fields(request, {"secret_id"})
                self._vault.revoke(caller, request["secret_id"],
                                   WORKER_ACTOR, PROJECT_ID)
                result = {"ok": True}
            elif op == "archive":
                _fields(request, {"secret_id"})
                self._vault.archive_secret(caller, request["secret_id"])
                result = {"ok": True}
            elif op == "restore":
                _fields(request, {"secret_id"})
                self._vault.restore_secret(caller, request["secret_id"])
                result = {"ok": True}
            else:
                raise VaultDenied()
            serialized = json.dumps(result, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8") + b"\n"
            if len(serialized) > MAX_RESPONSE:
                raise VaultUnavailable()
            return serialized
        except (VaultDenied, VaultUnavailable, sqlite3.Error, TypeError, ValueError):
            return b'{"ok":false,"error":"denied"}\n'


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: VaultUnixServer = self.server  # type: ignore[assignment]
        uid = server.peer_uid(self.request)
        raw = self.rfile.readline(MAX_REQUEST + 2)
        if not raw.endswith(b"\n") or len(raw) > MAX_REQUEST + 1:
            self.wfile.write(b'{"ok":false,"error":"denied"}\n')
            return
        response = server.protocol.process(uid, raw.rstrip(b"\n"))
        self.wfile.write(response)


class VaultUnixServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, sock: socket.socket, protocol: VaultProtocol):
        self.protocol = protocol
        super().__init__("", _Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()

    @staticmethod
    def peer_uid(conn: socket.socket) -> int:
        pid, uid, gid = struct.unpack("3i", conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return uid


def serve_from_systemd() -> None:
    """Start from a systemd socket unit; never bind arbitrary network ports."""
    if os.getenv("LISTEN_PID") != str(os.getpid()) or os.getenv("LISTEN_FDS") != "1":
        raise VaultUnavailable()
    credential_dir = os.getenv("CREDENTIALS_DIRECTORY")
    state_dir = os.getenv("STATE_DIRECTORY")
    if not credential_dir or not state_dir:
        raise VaultUnavailable()
    master = _credential_file(Path(credential_dir), "vault-master")
    import pwd
    worker_uid = pwd.getpwnam("ae-runtime").pw_uid
    vault = SecureVault(Path(state_dir), owner_id=OWNER_ID,
                        key_provider=lambda: master)
    socket_fd = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with VaultUnixServer(socket_fd, VaultProtocol(vault, worker_uid=worker_uid)) as server:
            server.serve_forever(poll_interval=0.5)
    finally:
        vault.close()


_BACKUP_NAME = re.compile(r"vault-[0-9]{14}-[0-9a-f]{16}\.aev\Z")


def backup_on_netcup(*, backup_root: Path = Path("/var/backups/ai-agent-engine-vault")) -> None:
    """Local encrypted snapshots only, as expressly chosen by the owner.

    The backup directory must already be a private, owner-managed distinct
    path on Netcup. This is NOT off-host disaster recovery.
    """
    credential_dir = os.getenv("CREDENTIALS_DIRECTORY")
    state_dir = os.getenv("STATE_DIRECTORY")
    runtime_dir = os.getenv("RUNTIME_DIRECTORY")
    if not credential_dir or not state_dir or not runtime_dir:
        raise VaultUnavailable()
    meta = os.lstat(backup_root)
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid()
            or stat.S_IMODE(meta.st_mode) != 0o700):
        raise VaultUnavailable()
    master = _credential_file(Path(credential_dir), "vault-master")
    backup_key = _credential_file(Path(credential_dir), "vault-backup")
    vault = SecureVault(Path(state_dir), owner_id=OWNER_ID,
                        key_provider=lambda: master)
    owner = VaultCaller(OWNER_ID, PROJECT_ID, "owner-netcup", True)
    try:
        encrypted = vault.encrypted_backup(owner, backup_key)
    finally:
        vault.close()

    # Prove restore before retaining or deleting any previous snapshots.
    with tempfile.TemporaryDirectory(prefix="vault-backup-check-", dir=runtime_dir) as scratch:
        restored = SecureVault.restore_backup(
            encrypted, backup_key, Path(scratch) / "restored",
            owner_id=OWNER_ID, key_provider=lambda: master)
        try:
            for item in restored.list_secrets(owner):
                restored.get_secret(owner, item.secret_id)
        finally:
            restored.close()

    name = time.strftime("vault-%Y%m%d%H%M%S-", time.gmtime()) + uuid4().hex[:16] + ".aev"
    directory_fd = os.open(backup_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            os.unlink(name, dir_fd=directory_fd)
            raise
        os.fsync(directory_fd)

        copies: list[str] = []
        for existing in os.listdir(directory_fd):
            if not _BACKUP_NAME.fullmatch(existing):
                continue
            item = os.stat(existing, dir_fd=directory_fd, follow_symlinks=False)
            if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
                    or item.st_uid != os.geteuid()
                    or stat.S_IMODE(item.st_mode) != 0o600):
                raise VaultUnavailable()
            copies.append(existing)
        for old in sorted(copies)[:-7]:
            os.unlink(old, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Netcup Vault private service")
    parser.add_argument("action", choices=("serve", "backup"))
    options = parser.parse_args(argv)
    try:
        if options.action == "serve":
            serve_from_systemd()
        else:
            backup_on_netcup()
            print("VAULT_BACKUP=VERIFIED_LOCAL_ONLY")
        return 0
    except (OSError, VaultDenied, VaultUnavailable, ValueError):
        print("VAULT_RUNTIME=UNAVAILABLE")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
