"""Owner-only, opt-in combined acceptance of GPT tools and official Claude Code.

The default 'check' performs no inference, uses no paid API key and does not
read or export OAuth credentials. 'run-live' must be requested explicitly by
the owner on the machine holding their personal sign-ins.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from .api_errors import InferenceError
from .plan_agent import run_one_click
from .session_refresh import SessionRefreshError
from .siwc import CredentialStore, DIRECT_SCOPE

CLAUDE_PROMPT = "Say exactly: Hello, world!"
CLAUDE_REPLY = "Hello, world!"
MAX_CLAUDE_JSON_BYTES = 65_536


class AcceptanceError(Exception):
    """Public, credential-free explanation of a blocked acceptance test."""


def _validate_environment(env: Mapping[str, str]) -> None:
    """Reject any environment that could select API billing or injected auth."""
    forbidden = [
        name for name, value in env.items()
        if value and (
            name.startswith("ANTHROPIC_")
            or name.startswith("CLAUDE_CODE_USE_")
            or name.startswith("CLAUDE_CODE_OAUTH_")
            or "API_KEY" in name and name.startswith("CLAUDE_")
        )
    ]
    if forbidden:
        raise AcceptanceError(
            "Claude environment contains provider/API authentication overrides; "
            "unset them before a subscription-only acceptance test"
        )


def check_gpt(store: CredentialStore | None = None) -> None:
    """Inspect only local session metadata; never refresh or send a request."""
    store = store or CredentialStore()
    session = store.load_session()
    if not isinstance(session, dict):
        raise AcceptanceError("GPT has no saved local ChatGPT sign-in")
    scopes = session.get("scopes")
    if (session.get("sharing_enabled") is not True or
            not isinstance(scopes, list) or
            DIRECT_SCOPE not in scopes or "resource.invoke" not in scopes):
        raise AcceptanceError("GPT sign-in lacks ChatGPT plan sharing permission")


def _claude_cli() -> str:
    executable = shutil.which("claude")
    if not executable:
        raise AcceptanceError("Official Claude Code CLI is not installed on this host")
    return executable


def _run_claude(args: list[str], *, cwd: str | None = None,
                timeout: int = 30,
                env: Mapping[str, str] | None = None) -> dict[str, Any]:
    current_env = dict(os.environ if env is None else env)
    _validate_environment(current_env)
    executable = _claude_cli()
    try:
        result = subprocess.run(
            [executable, *args],
            cwd=cwd, env=current_env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AcceptanceError("Official Claude Code CLI could not finish") from None
    if result.returncode != 0:
        raise AcceptanceError(
            "Official Claude Code CLI returned an error; no API fallback attempted"
        )
    if len(result.stdout) > MAX_CLAUDE_JSON_BYTES:
        raise AcceptanceError("Claude output exceeded the acceptance safety limit")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise AcceptanceError("Claude returned an invalid JSON result") from None
    if not isinstance(payload, dict):
        raise AcceptanceError("Claude returned an unexpected response structure")
    return payload


def check_claude(*, env: Mapping[str, str] | None = None) -> None:
    """Use the official read-only auth status, accept only claude.ai sign-in."""
    status = _run_claude(["auth", "status"], env=env)
    if status.get("authMethod") != "claude.ai":
        raise AcceptanceError(
            "Claude is not signed in with a personal claude.ai subscription; "
            "API/Console and third-party methods are intentionally rejected"
        )


def probe_claude(*, env: Mapping[str, str] | None = None) -> None:
    """One text-only official CLI turn, with no tools, MCP, plugins or hooks."""
    check_claude(env=env)
    with TemporaryDirectory(prefix="ai-agent-claude-accept-") as directory:
        result = _run_claude([
            "--bare", "-p", CLAUDE_PROMPT,
            "--output-format", "json",
            "--max-turns", "1",
            "--no-session-persistence",
            "--tools", "",
            "--disallowedTools", "*",
            "--permission-mode", "plan",
            "--no-chrome",
        ], cwd=directory, timeout=120, env=env)
    if (result.get("type") != "result" or
            result.get("subtype") != "success" or
            result.get("is_error") is not False or
            not isinstance(result.get("result"), str) or
            result["result"].strip() != CLAUDE_REPLY):
        raise AcceptanceError(
            "Claude did not return the expected completed text result"
        )


def run_live() -> None:
    """Preflight both accounts before the first live, subscription-metered call."""
    store = CredentialStore()
    check_gpt(store)
    check_claude()
    model, outcome, passed, safe_error = run_one_click(store)
    if not passed:
        raise AcceptanceError(
            "GPT sandbox tools were not verified"
            + (": " + safe_error if safe_error else "")
        )
    print("GPT native sandbox write/read: PASS (model:", model + ")")
    probe_claude()
    print("Claude Code personal subscription reply: PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Owner-operated GPT + official Claude Code acceptance"
    )
    parser.add_argument(
        "command", choices=["check", "run-live"],
        help="check is read-only; run-live explicitly consumes subscription allowances",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check_gpt()
            print("GPT ChatGPT-plan grant: saved locally")
            check_claude()
            print("Claude Code claude.ai login: present")
            print("No model requests were sent")
        else:
            run_live()
        return 0
    except (AcceptanceError, InferenceError, SessionRefreshError) as exc:
        print("Combined acceptance unavailable:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
