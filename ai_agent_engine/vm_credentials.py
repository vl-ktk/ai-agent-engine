"""Offline-only, owner-gated import of one SIWC session to a future self-hosted VM.

The transport is supplied by the owner (for example, an SSH stdin pipe).
This module never reads source credentials from the repository, exports a
session, connects to a server, revokes a token or invokes a model.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import sys
from typing import Any, BinaryIO, Mapping
import uuid

from .session_refresh import _session_lock, SessionRefreshError
from .siwc import AuthError, CredentialStore, DIRECT_SCOPE, ISSUER


MAX_IMPORT_BYTES = 128_000
SESSION_KEYS = frozenset({
    "issuer", "subject", "email", "client_id", "ext_agent_host_id",
    "id_token", "access_token", "refresh_token", "token_type", "scopes",
    "sharing_enabled", "expires_at", "saved_at",
})


class VMImportError(Exception):
    """Expected validation failure with a message safe for terminal output."""


def _valid_host(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("urn:uuid:"):
        return False
    try:
        return uuid.UUID(value[len("urn:uuid:"):]).version == 4
    except (ValueError, AttributeError):
        return False


def existing_vm_host(store: CredentialStore) -> str:
    """Read a previously initialized VM ID without accidentally creating one."""
    record = store._read("host.json")
    if not isinstance(record, dict):
        raise VMImportError("VM host ID is missing; prepare the destination first")
    host = record.get("ext_agent_host_id")
    if not _valid_host(host):
        raise VMImportError("VM host ID is invalid; manual recovery is required")
    return host


def prepare_host(store: CredentialStore) -> str:
    """Explicit setup, serialized to avoid two concurrent new host IDs."""
    with _session_lock(store):
        return store.host_id()


def _require_string(session: Mapping[str, Any], key: str, max_len: int) -> str:
    value = session.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise VMImportError("Credential input failed validation")
    return value


def validate_session(session: object, destination_host: str,
                     existing_client: str | None = None) -> dict[str, Any]:
    """Validate the local session-file format; no network/JWT proof is claimed.

    The source must already have completed genuine OAuth on a trusted computer.
    This validates a protected transport payload, not ownership of a token.
    """
    if not isinstance(session, dict) or set(session) != SESSION_KEYS:
        raise VMImportError("Credential input failed validation")
    if session.get("issuer") != ISSUER or session.get("token_type") != "Bearer":
        raise VMImportError("Credential input failed validation")
    if session.get("sharing_enabled") is not True:
        raise VMImportError("Credential input failed validation")
    source_host = _require_string(session, "ext_agent_host_id", 48)
    if not _valid_host(source_host) or not _valid_host(destination_host):
        raise VMImportError("Credential host ID failed validation")
    if source_host == destination_host:
        raise VMImportError("Source and destination must have distinct host IDs")
    client = _require_string(session, "client_id", 256)
    if client == "dynamic_agent_client" or (existing_client and client != existing_client):
        raise VMImportError("Issued OAuth client registration does not match destination")
    _require_string(session, "subject", 2048)
    for key in ("id_token", "access_token", "refresh_token"):
        _require_string(session, key, 32_000)
    email = session.get("email")
    if email is not None and (not isinstance(email, str) or len(email) > 320):
        raise VMImportError("Credential input failed validation")
    scopes = session.get("scopes")
    if (not isinstance(scopes, list) or len(scopes) > 100
            or any(not isinstance(scope, str) or not scope or len(scope) > 128
                   for scope in scopes)
            or DIRECT_SCOPE not in scopes or "resource.invoke" not in scopes):
        raise VMImportError("ChatGPT plan permission is missing")
    expiry = session.get("expires_at")
    if type(expiry) not in (float, int) or not math.isfinite(expiry):
        raise VMImportError("Credential input failed validation")
    timestamp = _require_string(session, "saved_at", 64)
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        raise VMImportError("Credential input failed validation") from None
    imported = dict(session)
    # The destination has its OWN permanent host ID, not the original PC ID.
    imported["ext_agent_host_id"] = destination_host
    imported["scopes"] = list(scopes)
    return imported


def read_session(stream: BinaryIO) -> object:
    if stream.isatty():
        raise VMImportError("Pipe a protected source session into stdin")
    raw = stream.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise VMImportError("Credential input exceeded size limit")
    try:
        return json.loads(raw)
    except (TypeError, UnicodeError, ValueError):
        raise VMImportError("Credential input is not valid JSON") from None


def inspect_import(store: CredentialStore, payload: object) -> None:
    """Dry-run: no directory creation, session write, token refresh or transport."""
    vm_host = existing_vm_host(store)
    if store.load_session() is not None:
        raise VMImportError("Destination already has a session; refusing overwrite")
    validate_session(payload, vm_host, store.client_id())


def import_session(store: CredentialStore, payload: object, *,
                   confirmed_host: str) -> None:
    """Explicit one-time import; the owner controls the secure transport."""
    if not confirmed_host or confirmed_host != existing_vm_host(store):
        raise VMImportError("Destination host confirmation failed")
    with _session_lock(store):
        host = existing_vm_host(store)  # Recheck after acquiring the shared lock.
        if host != confirmed_host:
            raise VMImportError("Destination host ID changed; import cancelled")
        if store.load_session() is not None:
            raise VMImportError("Destination already has a session; refusing overwrite")
        imported = validate_session(payload, host, store.client_id())
        store.save_session(imported)


def main(argv: list[str] | None = None, *, store: CredentialStore | None = None,
         input_stream: BinaryIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Owner-only ChatGPT VM session staging")
    parser.add_argument("command", choices=("prepare-host", "check", "validate-stdin", "import-stdin"))
    parser.add_argument("--apply", action="store_true", help="allow a one-time credential import")
    parser.add_argument("--confirm-vm-host", help="repeat the prepared destination urn:uuid ID")
    options = parser.parse_args(argv)
    try:
        target = store if store is not None else CredentialStore()
        if options.command == "prepare-host":
            if options.apply or options.confirm_vm_host:
                raise VMImportError("Unexpected options for host preparation")
            print("VM host ID:", prepare_host(target))
        elif options.command == "check":
            if options.apply or options.confirm_vm_host:
                raise VMImportError("Unexpected options for host check")
            print("Existing VM host ID:", existing_vm_host(target))
        else:
            if options.command == "validate-stdin" and (options.apply or options.confirm_vm_host):
                raise VMImportError("Dry-run does not accept apply options")
            if options.command == "import-stdin" and (
                    not options.apply or not options.confirm_vm_host):
                raise VMImportError("Import requires --apply and --confirm-vm-host")
            # Fail before receiving secret bytes if destination is unprepared.
            existing_vm_host(target)
            supplied = read_session(input_stream if input_stream is not None
                                    else sys.stdin.buffer)
            if options.command == "validate-stdin":
                inspect_import(target, supplied)
                print("DRY-RUN PASS: input format and destination checks passed; nothing imported")
            else:
                import_session(target, supplied, confirmed_host=options.confirm_vm_host)
                print("IMPORT COMPLETE: session installed; verify status on the VM")
        return 0
    except (VMImportError, AuthError, SessionRefreshError, OSError):
        # Never echo any exception detail containing a path or user-provided data.
        print("VM session operation unavailable; nothing was printed from credentials",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
