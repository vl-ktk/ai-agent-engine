"""Owner-controlled SIWC refresh for Linux, macOS and Windows.

No token is printed, sent to the agent, or stored in the source repository.
Refresh is serialized between local processes because OAuth refresh tokens rotate.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import time
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .siwc import CredentialStore, DIRECT_SCOPE, RESOURCE, TOKEN_URL


class SessionRefreshError(Exception):
    """Safe, redacted authentication error."""


@contextmanager
def _session_lock(store: CredentialStore, *, wait_seconds: float = 10.0) -> Iterator[None]:
    """Bounded interprocess lock for Linux/macOS/Windows token rotation.

    Lock the same persistent file for refresh and explicit logout. Never
    unlink the lock file: replacing its inode could permit two simultaneous
    refreshes. Windows uses a one-byte msvcrt lock; POSIX uses flock.
    """
    if os.name not in ("posix", "nt"):
        raise SessionRefreshError("Session refresh is unsupported on this platform")
    if not 0 <= wait_seconds <= 120:
        raise SessionRefreshError("Invalid credential lock timeout")
    store._ensure_root()
    path = store.root / ".refresh.lock"
    if path.is_symlink():
        raise SessionRefreshError("Credential lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise SessionRefreshError("Could not secure the session refresh lock") from None
    locked = False
    try:
        import errno
        import stat

        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SessionRefreshError("Credential lock must be a regular file")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        deadline = time.monotonic() + wait_seconds
        while True:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise SessionRefreshError("Credential lock acquisition failed") from None
                if time.monotonic() >= deadline:
                    raise SessionRefreshError("Timed out waiting for the credential lock") from None
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        yield
    except OSError:
        raise SessionRefreshError("Credential lock operation failed") from None
    finally:
        try:
            if locked:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def refresh_if_needed(store: CredentialStore | None = None, *,
                      opener: Callable[..., Any] = urlopen,
                      clock: Callable[[], float] = time.time) -> bool:
    """Refresh near expiry. Return True only when a new token set was saved.

    Refresh is performed only after the user explicitly runs a diagnostic task.
    A failed refresh leaves the prior local session untouched.
    """
    session_store = store or CredentialStore()
    with _session_lock(session_store):
        session = session_store.load_session()
        if not isinstance(session, dict):
            raise SessionRefreshError("No saved ChatGPT sign-in; owner sign-in is required")
        scopes = session.get("scopes")
        if (session.get("sharing_enabled") is not True or
                not isinstance(scopes, list) or
                DIRECT_SCOPE not in scopes or "resource.invoke" not in scopes):
            raise SessionRefreshError("ChatGPT plan permission is missing")
        client_id = session.get("client_id")
        refresh_token = session.get("refresh_token")
        expires_at = session.get("expires_at")
        if (not isinstance(client_id, str) or not client_id or
                client_id == "dynamic_agent_client" or
                not isinstance(refresh_token, str) or not refresh_token or
                type(expires_at) not in (int, float)):
            raise SessionRefreshError("Saved ChatGPT session cannot be refreshed")
        if expires_at - clock() > 90:
            return False

        form = urlencode({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "resource": RESOURCE,
        }).encode("utf-8")
        request = Request(TOKEN_URL, data=form, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }, method="POST")
        try:
            with opener(request, timeout=20) as response:
                raw = response.read(128_001)
        except HTTPError as exc:
            raise SessionRefreshError(f"ChatGPT session refresh was rejected (HTTP {exc.code})") from None
        except (URLError, TimeoutError, OSError):
            raise SessionRefreshError("ChatGPT session refresh was interrupted") from None
        if len(raw) > 128_000:
            raise SessionRefreshError("Invalid ChatGPT refresh response")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, TypeError):
            raise SessionRefreshError("Invalid ChatGPT refresh response") from None
        if not isinstance(payload, dict):
            raise SessionRefreshError("Invalid ChatGPT refresh response")
        token = payload.get("access_token")
        new_refresh_token = payload.get("refresh_token", refresh_token)
        expires_in = payload.get("expires_in")
        if (not isinstance(token, str) or not token or
                not isinstance(new_refresh_token, str) or not new_refresh_token or
                type(expires_in) not in (int, float) or not 30 < expires_in <= 86400 * 7 or
                str(payload.get("token_type", "Bearer")).lower() != "bearer"):
            raise SessionRefreshError("ChatGPT refresh returned invalid credentials")
        granted = scopes
        if "scope" in payload:
            raw_scope = payload["scope"]
            if not isinstance(raw_scope, str):
                raise SessionRefreshError("ChatGPT refresh returned invalid scopes")
            granted = sorted(set(raw_scope.split()))
            if DIRECT_SCOPE not in granted or "resource.invoke" not in granted:
                raise SessionRefreshError("ChatGPT plan permission is no longer granted")
        updated = dict(session)
        updated.update({
            "access_token": token,
            "refresh_token": new_refresh_token,
            "expires_at": int(clock() + expires_in),
            "scopes": granted,
            "sharing_enabled": True,
            "saved_at": datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat(),
        })
        session_store.save_session(updated)
        return True
