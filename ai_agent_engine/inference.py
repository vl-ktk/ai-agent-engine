"""Minimal ChatGPT-plan model probe for the owner's OSS installation.

Uses only the validated local SIWC session. Never prints or exports credentials.
This is a diagnostic CLI, not yet the agent runtime model adapter.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .siwc import CredentialStore, DIRECT_SCOPE

API_ORIGIN = "https://api.openai.com/v1"
MAX_MODEL_RESPONSE_BYTES = 2_000_000
MAX_STREAM_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 20_000


class InferenceError(Exception):
    """Safe diagnostic failure without raw credentials or provider bodies."""


def _access_token(store: CredentialStore, *, now: float | None = None) -> str:
    session = store.load_session()
    if not session:
        raise InferenceError("No saved ChatGPT sign-in. Run: python3 -m ai_agent_engine.auth login")
    scopes = session.get("scopes", [])
    if (session.get("sharing_enabled") is not True or
            not isinstance(scopes, list) or
            DIRECT_SCOPE not in scopes or "resource.invoke" not in scopes):
        raise InferenceError("Saved account lacks ChatGPT plan usage permission")
    token = session.get("access_token")
    expiry = session.get("expires_at")
    if not isinstance(token, str) or not token or type(expiry) not in (int, float):
        raise InferenceError("Saved ChatGPT credentials are invalid")
    if expiry - (time.time() if now is None else now) <= 30:
        raise InferenceError("ChatGPT access token expired or nearly expired; sign in again")
    return token


def _open(request: Request, *, opener: Callable[..., Any], timeout: int) -> Any:
    try:
        return opener(request, timeout=timeout)
    except HTTPError as exc:
        raise InferenceError(f"OpenAI request rejected (HTTP {exc.code})") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise InferenceError("OpenAI request could not be completed") from None


def _authorized_request(url: str, token: str, *, data: bytes | None = None) -> Request:
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return Request(url, data=data, headers=headers,
                   method="POST" if data is not None else "GET")


def list_models(store: CredentialStore | None = None, *,
                opener: Callable[..., Any] = urlopen) -> list[tuple[str, str]]:
    """Return ordered account-visible model slugs and display names."""
    token = _access_token(store or CredentialStore())
    request = _authorized_request(API_ORIGIN + "/models", token)
    try:
        with _open(request, opener=opener, timeout=20) as response:
            raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
        if len(raw) > MAX_MODEL_RESPONSE_BYTES:
            raise InferenceError("Model list is unexpectedly large")
        payload = json.loads(raw)
        rows = payload.get("models")
        if not isinstance(rows, list):
            raise InferenceError("OpenAI returned an invalid model list")
        results: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("visibility") != "list":
                continue
            slug = row.get("slug")
            if isinstance(slug, str) and slug and len(slug) <= 128 and slug not in seen:
                label = row.get("display_name")
                results.append((slug, label if isinstance(label, str) and label else slug))
                seen.add(slug)
        return results
    except (ValueError, UnicodeError, TypeError) as exc:
        raise InferenceError("OpenAI returned an invalid model list") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise InferenceError("Could not read OpenAI model list") from None


def _consume_sse(response: Any) -> str:
    """Only report an answer once a terminal response.completed event arrives."""
    total_bytes = 0
    parts: list[str] = []
    chars = 0
    completed = False
    data_lines: list[str] = []

    def dispatch() -> None:
        nonlocal completed, chars
        if not data_lines:
            return
        raw = "\n".join(data_lines)
        data_lines.clear()
        if raw == "[DONE]":
            return
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            raise InferenceError("OpenAI returned a malformed stream event") from None
        if not isinstance(event, dict):
            raise InferenceError("OpenAI returned an invalid stream event")
        kind = event.get("type")
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise InferenceError("OpenAI returned an invalid text delta")
            chars += len(delta)
            if chars > MAX_OUTPUT_CHARS:
                raise InferenceError("OpenAI response exceeded the diagnostic text limit")
            parts.append(delta)
        elif kind == "response.failed":
            error = event.get("response", {}).get("error") if isinstance(event.get("response"), dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            safe_code = code if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 80 else "unknown_error"
            raise InferenceError(f"OpenAI response failed ({safe_code})")
        elif kind == "response.incomplete":
            raise InferenceError("OpenAI response was incomplete")
        elif kind == "response.completed":
            completed = True

    for line in response:
        if not isinstance(line, bytes):
            raise InferenceError("OpenAI returned an invalid stream")
        total_bytes += len(line)
        if total_bytes > MAX_STREAM_BYTES:
            raise InferenceError("OpenAI response exceeded the diagnostic stream limit")
        clean = line.rstrip(b"\r\n")
        if not clean:
            dispatch()
            continue
        if clean.startswith(b"data:"):
            try:
                data_lines.append(clean[5:].lstrip(b" ").decode("utf-8"))
            except UnicodeError:
                raise InferenceError("OpenAI stream contains invalid encoding") from None
    dispatch()
    if not completed:
        raise InferenceError("OpenAI stream ended without response.completed")
    return "".join(parts)


def request_answer(model: str, prompt: str, store: CredentialStore | None = None, *,
                   opener: Callable[..., Any] = urlopen) -> str:
    """Use the user's Plus grant: streaming, stateless Responses only."""
    if not prompt.strip() or len(prompt) > 4000:
        raise InferenceError("Prompt must be 1 to 4000 nonblank characters")
    session_store = store or CredentialStore()
    available = list_models(session_store, opener=opener)
    if model not in {slug for slug, _ in available}:
        raise InferenceError("Requested model is not in this ChatGPT account's model list")
    token = _access_token(session_store)
    body = json.dumps({
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "store": False,
        "stream": True,
        "max_output_tokens": 128,
    }).encode("utf-8")
    request = _authorized_request(API_ORIGIN + "/responses", token, data=body)
    try:
        with _open(request, opener=opener, timeout=60) as response:
            return _consume_sse(response)
    except (URLError, TimeoutError, OSError) as exc:
        raise InferenceError("OpenAI response stream was interrupted") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ChatGPT Plus model probe (no API key)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("models")
    ask = sub.add_parser("ask")
    ask.add_argument("--model", required=True)
    ask.add_argument("--prompt", default="Say exactly: Hello, world!")
    args = parser.parse_args(argv)
    try:
        if args.command == "models":
            models = list_models()
            if not models:
                raise InferenceError("No visible models in this ChatGPT account's catalog")
            for slug, label in models:
                print(f"{slug}: {label}")
        else:
            print(request_answer(args.model, args.prompt))
        return 0
    except InferenceError as exc:
        print("Model probe unavailable:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
