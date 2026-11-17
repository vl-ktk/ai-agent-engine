"""Owner-only, opt-in combined acceptance of GPT tools and official Claude Code.

The default 'check' performs no inference, uses no paid API key and does not
read or export OAuth credentials. 'run-live' must be requested explicitly by
the owner on the machine holding their personal sign-ins.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from .api_errors import InferenceError
from .inference import list_models, request_answer
from .plan_agent import ToolCycleReport, run_tool_cycle
from .session_refresh import SessionRefreshError, refresh_if_needed
from .siwc import CredentialStore, DIRECT_SCOPE

CLAUDE_PROMPT = "Say exactly: Hello, world!"
CLAUDE_REPLY = "Hello, world!"
MAX_CLAUDE_JSON_BYTES = 65_536
GPT_REPLY_PROMPT = "Say exactly: Hello, world!"
PERSONAL_CLAUDE_PLANS = ("pro", "max")
# Inherited from a parent Claude Code session; never pass to the probe.
_CHILD_ENV_DROP_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE")


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


def _child_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Validate, then drop parent Claude Code session variables."""
    current = dict(os.environ if env is None else env)
    _validate_environment(current)
    return {k: v for k, v in current.items()
            if not k.startswith(_CHILD_ENV_DROP_PREFIXES)}


def _invoke_claude(args: list[str], *, cwd: str | None = None, timeout: int = 30,
                   env: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any] | None]:
    """Run the official CLI once. Never returns or prints stderr."""
    child_env = _child_env(env)
    executable = _claude_cli()
    try:
        result = subprocess.run(
            [executable, *args],
            cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise AcceptanceError(f"Official Claude Code CLI timed out after {timeout}s") from None
    except OSError:
        raise AcceptanceError("Official Claude Code CLI could not be started") from None
    if len(result.stdout) > MAX_CLAUDE_JSON_BYTES:
        raise AcceptanceError("Claude output exceeded the acceptance safety limit")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        payload = None
    return result.returncode, payload if isinstance(payload, dict) else None


def _run_claude(args: list[str], *, cwd: str | None = None,
                timeout: int = 30,
                env: Mapping[str, str] | None = None) -> dict[str, Any]:
    code, payload = _invoke_claude(args, cwd=cwd, timeout=timeout, env=env)
    if code != 0:
        raise AcceptanceError(
            "Official Claude Code CLI returned an error"
            + _claude_error_facts(payload) + "; no API fallback attempted"
        )
    if payload is None:
        raise AcceptanceError("Claude returned an invalid JSON result")
    return payload


_SAFE_TOKEN = __import__("re").compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def _claude_error_facts(payload: Mapping[str, Any] | None) -> str:
    """Only fixed-shape metadata from a CLI JSON result; never message text."""
    if not isinstance(payload, Mapping):
        return ""
    facts = []
    for key in ("subtype", "terminal_reason"):
        value = payload.get(key)
        if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
            facts.append(f"{key}={value}")
    if isinstance(payload.get("is_error"), bool):
        facts.append("is_error=" + str(payload["is_error"]).lower())
    status = payload.get("api_error_status")
    if type(status) is int:
        facts.append(f"api_error_status={status}")
    return " (" + "; ".join(facts) + ")" if facts else ""


def claude_status(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _run_claude(["auth", "status"], env=env)


def check_claude(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Use the official read-only auth status, accept only claude.ai sign-in."""
    status = claude_status(env=env)
    if status.get("authMethod") != "claude.ai" or status.get("loggedIn") is False:
        raise AcceptanceError(
            "Claude is not signed in with a personal claude.ai subscription; "
            "API/Console and third-party methods are intentionally rejected"
        )
    return status


def check_claude_subscription(status: Mapping[str, Any]) -> str:
    provider = status.get("apiProvider")
    if provider not in (None, "firstParty"):
        raise AcceptanceError("Claude Code is routed to a third-party provider")
    plan = status.get("subscriptionType")
    if plan not in PERSONAL_CLAUDE_PLANS:
        raise AcceptanceError(
            "Claude sign-in is not a personal Pro/Max subscription"
            + (f" (subscriptionType={plan})" if isinstance(plan, str) and _SAFE_TOKEN.fullmatch(plan) else "")
        )
    return plan


# No --bare: per official CLI help, --bare never reads OAuth and would require
# an API key, which is exactly what this subscription-only test forbids.
CLAUDE_PROBE_ARGS = [
    "-p", CLAUDE_PROMPT,
    "--output-format", "json",
    "--no-session-persistence",
    "--tools", "",
    "--strict-mcp-config",
    "--permission-mode", "plan",
    "--no-chrome",
]


def claude_request(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Exactly one text-only official CLI turn; no tools or MCP servers."""
    with TemporaryDirectory(prefix="ai-agent-claude-accept-") as directory:
        return _run_claude(CLAUDE_PROBE_ARGS, cwd=directory, timeout=120, env=env)


def check_claude_result(result: Mapping[str, Any]) -> None:
    if (result.get("type") != "result" or
            result.get("subtype") != "success" or
            result.get("is_error") is not False or
            not isinstance(result.get("result"), str)):
        raise AcceptanceError(
            "Claude did not return a completed result" + _claude_error_facts(result)
        )
    if result["result"].strip() != CLAUDE_REPLY:
        raise AcceptanceError("Claude completed, but the reply text was not the expected test phrase")


def probe_claude(*, env: Mapping[str, str] | None = None) -> None:
    """Backward-compatible single probe: status, plan, one request, result."""
    check_claude_subscription(check_claude(env=env))
    check_claude_result(claude_request(env=env))


# --------------------------------------------------------------------------
# Staged combined acceptance

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"
_EXPECTED = (AcceptanceError, InferenceError, SessionRefreshError)


@dataclass
class Stage:
    key: str
    title: str
    status: str = SKIPPED
    detail: str = ""


class Report:
    def __init__(self) -> None:
        self.stages: list[Stage] = []

    def add(self, key: str, title: str, status: str, detail: str = "") -> Stage:
        stage = Stage(key, title, status, detail)
        self.stages.append(stage)
        print(f"[{status}] {title}" + (f" — {detail}" if detail else ""), flush=True)
        return stage

    def skip_all(self, items: list[tuple[str, str]], reason: str) -> None:
        for key, title in items:
            self.add(key, title, SKIPPED, reason)

    @property
    def passed(self) -> bool:
        return bool(self.stages) and all(s.status == PASS for s in self.stages)

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "stages": [asdict(s) for s in self.stages]}


def _safe(exc: BaseException) -> str:
    if isinstance(exc, _EXPECTED):
        return str(exc)
    return "unexpected " + type(exc).__name__ + " (details withheld)"


GPT_TOOL_STAGES = [
    ("gpt.tool_write", "GPT: write_new_file tool call"),
    ("gpt.file_created", "GPT: created file verified on disk"),
    ("gpt.tool_read", "GPT: read_file tool call"),
    ("gpt.read_content", "GPT: read content verified"),
    ("gpt.loop_done", "GPT: agent loop completed"),
]
GPT_STAGES = [
    ("gpt.auth", "GPT: ChatGPT sign-in"),
    ("gpt.model", "GPT: model available"),
    ("gpt.reply", "GPT: real model reply"),
] + GPT_TOOL_STAGES
CLAUDE_STAGES = [
    ("claude.auth", "Claude: Claude Code sign-in"),
    ("claude.subscription", "Claude: personal subscription (no API key)"),
    ("claude.request", "Claude: real model request"),
    ("claude.completed", "Claude: completed reply"),
]


def _tool_stages(report: Report, cycle: ToolCycleReport) -> None:
    why = cycle.error or (cycle.tool_errors[0] if cycle.tool_errors else None)
    suffix = f" (agent status={cycle.status}; audit={', '.join(cycle.audit) or 'none'})"

    if cycle.write_executed:
        report.add(*GPT_TOOL_STAGES[0], PASS)
    elif "write_new_file" in cycle.requested_tools:
        report.add(*GPT_TOOL_STAGES[0], FAIL, (why or "tool rejected the call") + suffix)
    else:
        report.add(*GPT_TOOL_STAGES[0], FAIL,
                   (why or "GPT did not request write_new_file") + suffix)

    if cycle.file_content_ok:
        report.add(*GPT_TOOL_STAGES[1], PASS, "content matches exactly")
    elif cycle.file_exists:
        report.add(*GPT_TOOL_STAGES[1], FAIL,
                   "file exists but content differs from the expected test phrase"
                   + ("" if cycle.write_content_ok else " (GPT sent different content)"))
    elif cycle.write_executed:
        report.add(*GPT_TOOL_STAGES[1], FAIL,
                   "file not found at expected name"
                   + ("" if cycle.write_path_ok else " (GPT used a different path)"))
    else:
        report.add(*GPT_TOOL_STAGES[1], SKIPPED, "no file was written")

    if cycle.read_executed:
        report.add(*GPT_TOOL_STAGES[2], PASS)
    elif not cycle.write_executed:
        report.add(*GPT_TOOL_STAGES[2], SKIPPED, "write step did not succeed")
    else:
        report.add(*GPT_TOOL_STAGES[2], FAIL,
                   (why or ("tool rejected the call" if "read_file" in cycle.requested_tools
                            else "GPT did not request read_file")) + suffix)

    if cycle.read_output_ok:
        report.add(*GPT_TOOL_STAGES[3], PASS)
    elif cycle.read_executed:
        report.add(*GPT_TOOL_STAGES[3], FAIL, "read output differs from the expected test phrase")
    else:
        report.add(*GPT_TOOL_STAGES[3], SKIPPED, "read step did not succeed")

    if cycle.status == "done" and cycle.final_answer:
        report.add(*GPT_TOOL_STAGES[4], PASS,
                   f"{cycle.requests_sent} model request(s)")
    else:
        report.add(*GPT_TOOL_STAGES[4], FAIL, (why or "agent did not finish") + suffix)


def run_gpt_stages(report: Report, store: CredentialStore, *,
                   opener: Any = None) -> None:
    kwargs = {} if opener is None else {"opener": opener}
    try:
        check_gpt(store)
        refreshed = refresh_if_needed(store, **kwargs)
        report.add("gpt.auth", "GPT: ChatGPT sign-in", PASS,
                   "plan permission granted" + ("; access token refreshed" if refreshed else ""))
    except Exception as exc:
        report.add("gpt.auth", "GPT: ChatGPT sign-in", FAIL, _safe(exc))
        report.skip_all(GPT_STAGES[1:], "GPT sign-in failed")
        return
    try:
        models = list_models(store, **kwargs)
        if not models:
            raise AcceptanceError("No ChatGPT models are visible for this account")
        slug = models[0][0]
        report.add("gpt.model", "GPT: model available", PASS,
                   f"{slug} (first of {len(models)} visible)")
    except Exception as exc:
        report.add("gpt.model", "GPT: model available", FAIL, _safe(exc))
        report.skip_all(GPT_STAGES[2:], "no model selected")
        return
    try:
        reply = request_answer(slug, GPT_REPLY_PROMPT, store, **kwargs)
        if not reply.strip():
            raise AcceptanceError("GPT completed with an empty reply")
        exact = reply.strip() == CLAUDE_REPLY
        report.add("gpt.reply", "GPT: real model reply", PASS,
                   "response.completed received" + ("; text matches" if exact else "; text differs from test phrase"))
    except Exception as exc:
        report.add("gpt.reply", "GPT: real model reply", FAIL, _safe(exc))
        report.skip_all(GPT_TOOL_STAGES, "basic GPT reply failed; tool test not attempted")
        return
    try:
        cycle = run_tool_cycle(slug, store, **kwargs)
    except Exception as exc:
        report.add(*GPT_TOOL_STAGES[0], FAIL, _safe(exc))
        report.skip_all(GPT_TOOL_STAGES[1:], "tool cycle could not start")
        return
    _tool_stages(report, cycle)


def run_claude_stages(report: Report, *, env: Mapping[str, str] | None = None) -> None:
    try:
        status = check_claude(env=env)
        report.add("claude.auth", "Claude: Claude Code sign-in", PASS, "authMethod=claude.ai")
    except Exception as exc:
        report.add("claude.auth", "Claude: Claude Code sign-in", FAIL, _safe(exc))
        report.skip_all(CLAUDE_STAGES[1:], "Claude sign-in failed")
        return
    try:
        plan = check_claude_subscription(status)
        report.add("claude.subscription", "Claude: personal subscription (no API key)", PASS,
                   f"subscriptionType={plan}; no ANTHROPIC_* overrides")
    except Exception as exc:
        report.add("claude.subscription", "Claude: personal subscription (no API key)", FAIL, _safe(exc))
        report.skip_all(CLAUDE_STAGES[2:], "subscription not confirmed; request not sent")
        return
    try:
        result = claude_request(env=env)
        report.add("claude.request", "Claude: real model request", PASS, "CLI exited successfully")
    except Exception as exc:
        report.add("claude.request", "Claude: real model request", FAIL, _safe(exc))
        report.skip_all(CLAUDE_STAGES[3:], "request failed")
        return
    try:
        check_claude_result(result)
        report.add("claude.completed", "Claude: completed reply", PASS, "text matches")
    except Exception as exc:
        report.add("claude.completed", "Claude: completed reply", FAIL, _safe(exc))


def run_live(*, store: CredentialStore | None = None,
             env: Mapping[str, str] | None = None, opener: Any = None) -> Report:
    """One combined pass. No retries; GPT and Claude are independent."""
    report = Report()
    run_gpt_stages(report, store or CredentialStore(), opener=opener)
    run_claude_stages(report, env=env)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Owner-operated GPT + official Claude Code acceptance"
    )
    parser.add_argument(
        "command", choices=["check", "run-live"],
        help="check is read-only; run-live explicitly consumes subscription allowances",
    )
    parser.add_argument("--report-json", metavar="PATH",
                        help="also save the redacted stage report as JSON")
    args = parser.parse_args(argv)
    if args.command == "check":
        try:
            check_gpt()
            print("GPT ChatGPT-plan grant: saved locally")
            check_claude_subscription(check_claude())
            print("Claude Code claude.ai login: present")
            print("No model requests were sent")
            return 0
        except _EXPECTED as exc:
            print("Combined acceptance unavailable:", exc, file=sys.stderr)
            return 1
    print("Combined LIVE acceptance: one pass, no automatic retries", flush=True)
    report = run_live()
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as handle:
            json.dump(report.as_dict(), handle, indent=2)
    counts = {k: sum(s.status == k for s in report.stages) for k in (PASS, FAIL, SKIPPED)}
    print(f"Result: {'PASS' if report.passed else 'FAIL'} "
          f"({counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIPPED]} skipped)")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
