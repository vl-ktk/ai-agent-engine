"""Read-only Stage 6.14 preflight on existing Netcup, with no model requests.

Run only from an owner-reviewed Netcup admin request. Never expose
credential bytes, account identifiers, home directories, or CLI output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import subprocess

ROOT = Path("/opt/ai-agent-engine")
RELEASE = re.compile(r"[0-9a-f]{40}\Z")
USERS = ("ae-runtime", "ae-deploy")


def gpt_status(stdout: str, returncode: int) -> dict[str, bool]:
    """Parse known safe indicators, never publish the original response."""
    return {
        "status_usable": returncode == 0,
        "saved_session": returncode == 0 and "Saved ChatGPT identity:" in stdout,
        "plan_granted": returncode == 0 and "ChatGPT plan permission: GRANTED" in stdout,
    }


def claude_status(stdout: str, returncode: int) -> dict[str, bool]:
    """Read only narrowly scoped authorization booleans from official CLI."""
    try:
        body = json.loads(stdout)
    except (ValueError, UnicodeError):
        return {"status_usable": False, "subscription_eligible": False}
    if not isinstance(body, dict):
        return {"status_usable": False, "subscription_eligible": False}
    return {
        "status_usable": returncode == 0,
        "subscription_eligible": (
            returncode == 0 and body.get("loggedIn") is True
            and body.get("authMethod") == "claude.ai"
            and body.get("apiProvider") in (None, "firstParty")
            and body.get("subscriptionType") in ("pro", "max")
        ),
    }


def run_as(user: str, command: list[str], *, home: str, timeout: int) -> tuple[int, str]:
    """No root or API environment forwarded to an account subprocess."""
    env = [
        "/usr/bin/env", "-i",
        "HOME=" + home,
        "USER=" + user,
        "LOGNAME=" + user,
        "LANG=C.UTF-8",
        "PATH=" + home + "/.local/bin:" + home + "/.npm-global/bin:"
        "/usr/local/bin:/usr/bin:/bin",
    ]
    try:
        done = subprocess.run(
            ["/usr/sbin/runuser", "-u", user, "--", *env, *command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=timeout,
            check=False, errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""
    return done.returncode, done.stdout[:16000]


def inspect_user(user: str, python: str) -> dict[str, object]:
    home = pwd.getpwnam(user).pw_dir
    answer: dict[str, object] = {"user": user, "gpt": {}, "claude": {}}
    code, output = run_as(
        user, [python, "-I", "-m", "ai_agent_engine.auth", "status"],
        home=home, timeout=20,
    )
    answer["gpt"] = gpt_status(output, code)
    # The executable lookup is performed under the same reduced user context.
    lookup = (
        "import shutil; print('CLI_PRESENT' if shutil.which('claude') "
        "else 'CLI_MISSING')"
    )
    rc, found = run_as(
        user, ["/usr/bin/python3", "-I", "-c", lookup], home=home, timeout=10,
    )
    present = rc == 0 and found.strip() == "CLI_PRESENT"
    if not present:
        answer["claude"] = {
            "cli_present": False, "status_usable": False,
            "subscription_eligible": False,
        }
    else:
        rc, output = run_as(
            user, ["claude", "auth", "status"], home=home, timeout=25,
        )
        answer["claude"] = {"cli_present": True, **claude_status(output, rc)}
    return answer


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("refused: owner admin access required")
    previous = os.environ.get("AE_EXPECTED_CURRENT", "")
    if not RELEASE.fullmatch(previous):
        raise SystemExit("refused: expected release identity missing")
    if os.readlink(ROOT / "current") != "releases/" + previous:
        raise SystemExit("refused: active VPS release changed")
    subprocess.run(
        ["systemctl", "is-active", "--quiet", "ae-controller.service",
         "ae-backup.timer", "ae-host-check.timer"],
        check=True, timeout=15,
    )
    python = str(ROOT / "current" / "venv/bin/python")
    if not Path(python).is_file():
        raise SystemExit("refused: installed release interpreter missing")
    checks = [inspect_user(user, python) for user in USERS]
    eligible = any(
        entry["gpt"]["saved_session"] and entry["gpt"]["plan_granted"]
        and entry["claude"]["subscription_eligible"]
        for entry in checks
    )
    # This is NOT a paid model call and NOT a live 6.14 PASS.
    print("STAGE6_14_NETCUP_AUTH_PREFLIGHT " + json.dumps(
        {"status": "READY_FOR_OWNER_LIVE_TEST" if eligible else "AUTH_NOT_READY",
         "provider_users": checks, "models_invoked": False,
         "production_changes": False}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
