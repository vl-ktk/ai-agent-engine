"""Read-only Stage 6.14 provider login prerequisites on the existing VPS.

The report contains only booleans. It never reads saved sessions, touches
account tokens, starts a provider login, or sends model requests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import subprocess


ROOT = Path("/opt/ai-agent-engine")
USER = "ae-runtime"
EXPECTED_RELEASE = os.environ.get("AE_EXPECTED_CURRENT", "")
RELEASE_PATTERN = re.compile(r"[a-f0-9]{40}\Z")


def run_as_runtime(command: list[str], home: str, *, timeout: int = 20) -> tuple[int, str]:
    env = [
        "/usr/bin/env", "-i", "HOME=" + home, "USER=" + USER,
        "LOGNAME=" + USER, "LANG=C.UTF-8",
        "PATH=" + home + "/.local/bin:/usr/local/bin:/usr/bin:/bin",
    ]
    try:
        result = subprocess.run(
            ["/usr/sbin/runuser", "-u", USER, "--", *env, *command],
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=timeout,
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""
    return result.returncode, result.stdout[:4096]


def parse_import_report(raw: str, returncode: int) -> dict[str, bool]:
    """Only accept a fixed boolean schema from the unprivileged subprocess."""
    names = ("engine", "siwc", "jwt", "cryptography")
    empty = {name: False for name in names}
    if returncode:
        return empty
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return empty
    if not isinstance(value, dict) or set(value) != set(names):
        return empty
    if any(type(value[name]) is not bool for name in names):
        return empty
    return {name: value[name] for name in names}


def check_release_and_services() -> None:
    if os.geteuid() != 0:
        raise SystemExit("requires authorized root administrative job")
    if not RELEASE_PATTERN.fullmatch(EXPECTED_RELEASE):
        raise SystemExit("expected active release missing")
    if os.readlink(ROOT / "current") != "releases/" + EXPECTED_RELEASE:
        raise SystemExit("active release changed; aborting")
    subprocess.run(
        ["systemctl", "is-active", "--quiet", "ae-controller.service",
         "ae-backup.timer", "ae-host-check.timer"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=15,
    )


def main() -> None:
    check_release_and_services()
    python = ROOT / "current/venv/bin/python"
    if not python.is_file():
        raise SystemExit("release Python interpreter missing")
    home = pwd.getpwnam(USER).pw_dir
    # Use the exact same venv used by the deployed release, under ae-runtime.
    check_modules = (
        "import importlib.util,json; "
        "print(json.dumps({"
        "'engine':importlib.util.find_spec('ai_agent_engine') is not None,"
        "'siwc':importlib.util.find_spec('ai_agent_engine.siwc') is not None,"
        "'jwt':importlib.util.find_spec('jwt') is not None,"
        "'cryptography':importlib.util.find_spec('cryptography') is not None"
        "}))"
    )
    rc, raw = run_as_runtime([str(python), "-I", "-c", check_modules], home)
    modules = parse_import_report(raw, rc)
    claude_rc, _ = run_as_runtime(["claude", "--version"], home, timeout=35)
    claude_present = claude_rc == 0
    check_release_and_services()
    ready_gpt = all(modules.values())
    print("STAGE6_14_AUTH_PREREQS " + json.dumps({
        "status": "AUTH_COMMANDS_READY" if ready_gpt and claude_present
                  else "AUTH_PREREQS_INCOMPLETE",
        "gpt": {"installed_modules": modules, "ready_to_sign_in": ready_gpt},
        "claude": {"cli_ready_to_sign_in": claude_present},
        "release_unchanged": True, "services_healthy": True,
        "provider_logins_attempted": False, "models_invoked": False,
        "credentials_accessed": False, "production_changes": False,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
