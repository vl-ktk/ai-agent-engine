"""Safe local ChatGPT sign-out for the owner-controlled SIWC pilot.

This module is never run automatically. It keeps credentials private, attempts
remote revocation of the renewable session, and clears local tokens even when
the provider is temporarily unreachable.
"""

from __future__ import annotations

from contextlib import closing
import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .session_refresh import SessionRefreshError, _session_lock
from .siwc import AuthError, CredentialStore, ISSUER

DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"


def _read_discovery(opener: Callable[..., Any]) -> str:
    request = Request(DISCOVERY_URL, headers={"Accept": "application/json"})
    with opener(request, timeout=15) as response:
        raw = response.read(32_769)
    if len(raw) > 32_768:
        raise ValueError("oversized discovery document")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("issuer") != ISSUER:
        raise ValueError("unexpected issuer")
    endpoint = payload.get("revocation_endpoint")
    if not isinstance(endpoint, str):
        raise ValueError("missing revocation endpoint")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.hostname != "auth.openai.com"
            or parsed.port is not None or parsed.username is not None
            or parsed.password is not None or not parsed.path.startswith("/")
            or parsed.query or parsed.fragment):
        raise ValueError("unsafe revocation endpoint")
    return endpoint


def sign_out(store: CredentialStore | None = None, *,
             opener: Callable[..., Any] = urlopen,
             sleep: Callable[[float], None] = time.sleep) -> bool:
    """Stop using local tokens, and try to revoke the remote refresh token.

    Returns True for verified HTTP 200 remote revocation or an already-absent
    session; returns False if remote revocation could not be confirmed.
    Never returns or prints token values. An unconfirmed remote revocation must
    be shown to the owner so they can disconnect the app in ChatGPT Settings.
    """
    local_store = store or CredentialStore()
    try:
        with _session_lock(local_store):
            session = local_store.load_session()
            if session is None:
                return True
            client_id = session.get("client_id")
            refresh_token = session.get("refresh_token")
            confirmed = False
            if (isinstance(client_id, str) and client_id
                    and client_id != "dynamic_agent_client"
                    and isinstance(refresh_token, str) and refresh_token):
                try:
                    endpoint = _read_discovery(opener)
                    form = urlencode({
                        "token": refresh_token,
                        "token_type_hint": "refresh_token",
                        "client_id": client_id,
                    }).encode("utf-8")
                    request = Request(
                        endpoint, data=form,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                        method="POST",
                    )
                    for attempt in range(3):
                        try:
                            with opener(request, timeout=15) as response:
                                # An empty HTTP 200 is the documented success response.
                                confirmed = response.status == 200
                            if confirmed:
                                break
                            if attempt < 2:
                                sleep(0.25 * (2 ** attempt))
                        except HTTPError as exc:
                            if exc.code < 500:
                                break
                            if attempt < 2:
                                sleep(0.25 * (2 ** attempt))
                        except (URLError, TimeoutError, OSError):
                            if attempt < 2:
                                sleep(0.25 * (2 ** attempt))
                except (HTTPError, URLError, TimeoutError, OSError,
                        ValueError, TypeError, UnicodeError):
                    confirmed = False
            # Local logout must not silently retain usable tokens when the
            # remote revocation endpoint is unavailable.
            local_store.clear_session()
            return confirmed
    except SessionRefreshError as exc:
        raise AuthError("Cannot secure ChatGPT sign-out on this host") from None
