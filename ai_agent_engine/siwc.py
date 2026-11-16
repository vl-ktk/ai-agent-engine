"""Local Sign in with ChatGPT for the owner's open-source agent installation.

Only OAuth is implemented here. This module never invokes a model or starts a
server accessible from another machine. Tokens never enter agent prompts.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid
import webbrowser


ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/auth/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "AI Agent Engine"


class AuthError(Exception):
    """An expected login failure, always safe to show without token details."""


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, _base64url(hashlib.sha256(verifier.encode("ascii")).digest())


def default_data_dir() -> Path:
    """Application data outside the source tree; separate per OS user."""
    if os.name == "nt":
        location = os.environ.get("LOCALAPPDATA")
        if not location:
            raise AuthError("LOCALAPPDATA is unavailable on this Windows account")
        return Path(location) / "AI Agent Engine"
    return Path.home() / ".config" / "ai-agent-engine"


class CredentialStore:
    """Single-account local pilot; refuse to switch identities implicitly."""

    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else default_data_dir()

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise AuthError("Credential directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)

    def _read(self, filename: str) -> dict[str, Any] | None:
        path = self.root / filename
        if path.is_symlink():
            raise AuthError("Credential file must not be a symlink")
        if not path.exists():
            return None
        if path.stat().st_size > 128_000:
            raise AuthError("Credential record is unexpectedly large")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError, OSError) as exc:
            raise AuthError("Credential record cannot be read") from exc
        if not isinstance(result, dict):
            raise AuthError("Credential record is invalid")
        return result

    def _write(self, filename: str, value: Mapping[str, Any]) -> None:
        self._ensure_root()
        destination = self.root / filename
        if destination.is_symlink():
            raise AuthError("Credential file must not be a symlink")
        descriptor, scratch = tempfile.mkstemp(prefix=".write-", dir=self.root)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(dict(value), handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(scratch, destination)
            if os.name != "nt":
                destination.chmod(0o600)
        finally:
            if os.path.exists(scratch):
                os.unlink(scratch)

    def host_id(self) -> str:
        existing = self._read("host.json")
        if existing is not None:
            value = existing.get("ext_agent_host_id")
            if isinstance(value, str) and value.startswith("urn:uuid:"):
                try:
                    if uuid.UUID(value.removeprefix("urn:uuid:")).version == 4:
                        return value
                except ValueError:
                    pass
            raise AuthError("Saved host ID is invalid; do not silently regenerate it")
        value = "urn:uuid:" + str(uuid.uuid4())
        self._write("host.json", {"ext_agent_host_id": value})
        return value

    def load_session(self) -> dict[str, Any] | None:
        return self._read("session.json")

    def client_id(self) -> str | None:
        session = self.load_session()
        pending = self._read("pending-registration.json")
        value = (session or pending or {}).get("client_id")
        if value is None:
            return None
        if not isinstance(value, str) or not value or value == "dynamic_agent_client":
            raise AuthError("Saved issued client ID is invalid")
        return value

    def remember_registration(self, client_id: str) -> None:
        if not client_id or client_id == "dynamic_agent_client" or len(client_id) > 256:
            raise AuthError("OpenAI returned an invalid client ID")
        existing = self.client_id()
        if existing is not None and existing != client_id:
            raise AuthError("Client ID changed; refusing to overwrite registration")
        self._write("pending-registration.json", {"client_id": client_id})

    def save_session(self, session: Mapping[str, Any]) -> None:
        previous = self.load_session()
        if previous is not None and (
            previous.get("client_id") != session.get("client_id")
            or previous.get("subject") != session.get("subject")
        ):
            raise AuthError("Account identity changed; refusing to overwrite session")
        self._write("session.json", session)
        pending = self.root / "pending-registration.json"
        if pending.is_file() and not pending.is_symlink():
            pending.unlink()


def authorization_url(*, redirect_uri: str, state: str, nonce: str,
                      code_challenge: str, host_id: str,
                      issued_client_id: str | None = None) -> str:
    if not redirect_uri.startswith("http://127.0.0.1:") or not redirect_uri.endswith(CALLBACK_PATH):
        raise ValueError("redirect_uri must be the local loopback callback")
    params = {
        "client_id": issued_client_id or "dynamic_agent_client",
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    if issued_client_id is None:
        params["agent_name_hint"] = APP_NAME
    return AUTHORIZE_URL + "?" + urlencode(params)


class _CallbackHandler(BaseHTTPRequestHandler):
    """No HTTP request logging: callback URLs contain a one-time OAuth code."""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _CallbackServer)
        route = urlsplit(self.path)
        expected_host = f"127.0.0.1:{server.server_port}"
        if route.path != CALLBACK_PATH or self.headers.get("Host") != expected_host or len(self.path) > 4096:
            self._reply(404, "Invalid callback")
            return
        params = parse_qs(route.query, keep_blank_values=True)
        state = params.get("state", [])
        if len(state) != 1 or not secrets.compare_digest(state[0], server.expected_state):
            self._reply(400, "Invalid authorization state")
            return
        if any(len(values) != 1 for values in params.values()):
            self._reply(400, "Invalid callback parameters")
            return
        if "code" not in params and "error" not in params:
            self._reply(400, "Missing authorization result")
            return
        if "code" in params and "error" in params:
            self._reply(400, "Conflicting authorization result")
            return
        server.result = {key: values[0] for key, values in params.items()}
        self._reply(200, "Return to the AI Agent Engine terminal for the result")

    def _reply(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        self.wfile.write(body)


class _CallbackServer(HTTPServer):
    def __init__(self, expected_state: str):
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.expected_state = expected_state
        self.result: dict[str, str] | None = None
        self.timeout = 1.0

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server_port}{CALLBACK_PATH}"

    def wait_for_result(self, timeout: float = 300) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        while self.result is None and time.monotonic() < deadline:
            self.handle_request()
        if self.result is None:
            raise AuthError("Authorization timed out; start the login again")
        return self.result


def _exchange_code(*, client_id: str, code: str, verifier: str,
                   redirect_uri: str) -> dict[str, Any]:
    fields = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": RESOURCE,
    }
    request = Request(TOKEN_URL, data=urlencode(fields).encode("utf-8"), headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
    }, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise AuthError("OAuth token exchange failed")
            result = json.load(response)
    except HTTPError as exc:
        raise AuthError(f"OAuth token exchange rejected (HTTP {exc.code})") from None
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        raise AuthError("OAuth token exchange could not be completed") from exc
    if not isinstance(result, dict):
        raise AuthError("OAuth returned an invalid token response")
    return result


def validate_id_token(id_token: str, *, client_id: str, nonce: str,
                      signing_key: object | None = None) -> dict[str, Any]:
    """Require a verified OpenAI OIDC identity; never trust decoded JWT alone."""
    try:
        import jwt
    except ImportError as exc:
        raise AuthError('Install the optional dependency: pip install -r requirements-siwc.txt') from exc
    try:
        key = signing_key
        if key is None:
            key = jwt.PyJWKClient(JWKS_URL).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token, key=key, algorithms=["RS256", "ES256"],
            issuer=ISSUER, audience=client_id, leeway=5,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
        )
    except Exception as exc:
        raise AuthError("OpenAI ID token signature or claims could not be validated") from exc
    if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
        raise AuthError("OpenAI ID token nonce does not match this login")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise AuthError("OpenAI ID token has no valid subject")
    return claims


def validated_session(token_response: Mapping[str, Any], *, client_id: str,
                      host_id: str, nonce: str,
                      signing_key: object | None = None) -> dict[str, Any]:
    for key in ("id_token", "access_token", "refresh_token"):
        if not isinstance(token_response.get(key), str) or not token_response[key]:
            raise AuthError("OAuth did not return the required identity and session tokens")
    if str(token_response.get("token_type", "Bearer")).lower() != "bearer":
        raise AuthError("OAuth returned an unsupported token type")
    raw_scopes = token_response.get("scope", "")
    if not isinstance(raw_scopes, str):
        raise AuthError("OAuth did not return valid granted scopes")
    claims = validate_id_token(token_response["id_token"], client_id=client_id,
                               nonce=nonce, signing_key=signing_key)
    expires_in = token_response.get("expires_in", 0)
    if type(expires_in) not in (int, float) or not 0 < expires_in <= 86400 * 7:
        raise AuthError("OAuth returned an invalid access-token lifetime")
    granted = sorted(set(raw_scopes.split()))
    now = time.time()
    return {
        "issuer": ISSUER, "subject": claims["sub"],
        "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
        "client_id": client_id, "ext_agent_host_id": host_id,
        "id_token": token_response["id_token"],
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "token_type": "Bearer", "scopes": granted,
        "sharing_enabled": DIRECT_SCOPE in granted and "resource.invoke" in granted,
        "expires_at": int(now + expires_in),
        "saved_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }


def login(store: CredentialStore | None = None, *, timeout: float = 300) -> dict[str, Any]:
    """Interactive local-only login. Must run on the user's browser-equipped PC."""
    try:
        import jwt  # noqa: F401; fail before opening the browser if extras are missing.
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
    except ImportError as exc:
        raise AuthError("Install SIWC dependencies: pip install -r requirements-siwc.txt") from exc
    store = store or CredentialStore()
    existing = store.load_session()
    issued_client_id = store.client_id()
    host_id = store.host_id()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce()
    with _CallbackServer(state) as callback:
        url = authorization_url(
            redirect_uri=callback.redirect_uri, state=state, nonce=nonce,
            code_challenge=challenge, host_id=host_id,
            issued_client_id=issued_client_id,
        )
        print("Opening the browser for Continue with ChatGPT…")
        if not webbrowser.open(url, new=1):
            print("Open this URL in a browser on THIS computer (never send it to anyone):")
            print(url)
        result = callback.wait_for_result(timeout=timeout)
        if "error" in result:
            raise AuthError("OpenAI authorization was denied or cancelled")
        code = result.get("code")
        if not code:
            raise AuthError("OpenAI callback had no authorization code")
        callback_id = result.get("client_id")
        if issued_client_id:
            if callback_id and callback_id != issued_client_id:
                raise AuthError("OAuth callback client ID does not match saved registration")
            client_id = issued_client_id
        else:
            if not callback_id:
                raise AuthError("OpenAI did not issue a client ID during registration")
            client_id = callback_id
        # Keep this issued ID even if the one-time code exchange fails.
        store.remember_registration(client_id)
        response = _exchange_code(client_id=client_id, code=code,
                                  verifier=verifier, redirect_uri=callback.redirect_uri)
        session = validated_session(response, client_id=client_id,
                                    host_id=host_id, nonce=nonce)
        if existing and existing.get("subject") != session["subject"]:
            raise AuthError("Different ChatGPT account selected; saved session was not replaced")
        store.save_session(session)
        return {"email": session["email"], "sharing_enabled": session["sharing_enabled"],
                "expires_at": session["expires_at"]}
