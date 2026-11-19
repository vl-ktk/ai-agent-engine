"""Report owner-only GPT/Claude sign-in readiness without printing credentials.

All authentication CLI output is captured and reduced to fixed booleans.
No token, email, account identifier, URL or private file content is logged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import subprocess


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
USER = 'ae-runtime'
SIWC_PYTHON = ROOT / 'owner-siwc-6-14/venv/bin/python'


def run(argv: list[str], timeout: int = 25) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout, errors='replace')
        return p.returncode == 0, p.stdout[:4000]
    except (OSError, subprocess.TimeoutExpired):
        return False, ''


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('expected owner release missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if not run(['systemctl', 'is-active', '--quiet', unit])[0]:
            raise SystemExit('production unit inactive')
    if not SIWC_PYTHON.is_file():
        raise SystemExit('private owner SIWC venv missing')

    home = pwd.getpwnam(USER).pw_dir
    clean_env = [
        '/usr/bin/env', '-i', 'HOME=' + home, 'USER=' + USER,
        'LOGNAME=' + USER, 'LANG=C.UTF-8',
        'PATH=' + home + '/.local/bin:/usr/local/bin:/usr/bin:/bin',
    ]

    def owner(argv: list[str]) -> tuple[bool, str]:
        return run(['/usr/sbin/runuser', '-u', USER, '--',
                    *clean_env, *argv])

    gpt_ok, gpt_output = owner([
        str(SIWC_PYTHON), '-I', '-m', 'ai_agent_engine.auth', 'status',
    ])
    gpt_identity = gpt_ok and 'Saved ChatGPT identity:' in gpt_output
    gpt_permission = gpt_identity and 'ChatGPT plan permission: GRANTED' in gpt_output
    gpt_expired = 'Access token expired.' in gpt_output
    gpt_ready = gpt_permission and not gpt_expired

    claude_ok, claude_output = owner(['claude', 'auth', 'status', '--json'])
    claude_logged_in = False
    if claude_ok:
        try:
            data = json.loads(claude_output)
            claude_logged_in = isinstance(data, dict) and data.get('loggedIn') is True
        except (ValueError, TypeError):
            pass

    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed during status check')
    print('STAGE6_14_OWNER_AUTH_STATUS ' + json.dumps({
        'status': 'GPT_READY' if gpt_ready else 'GPT_AUTH_INCOMPLETE',
        'gpt': {
            'status_command_ok': gpt_ok,
            'saved_identity_present': gpt_identity,
            'plan_permission_granted': gpt_permission,
            'access_token_expired': gpt_expired,
            'ready_for_live_after_owner_approval': gpt_ready,
        },
        'claude': {
            'status_command_ok': claude_ok,
            'logged_in': claude_logged_in,
        },
        'credentials_printed': False,
        'emails_printed': False,
        'model_calls': False,
        'production_release_unchanged': True,
        'production_services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
