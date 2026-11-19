"""Open an interactive GPT SIWC login terminal on the private VPS desktop.

The owner signs in inside Firefox on the same VPS as the localhost callback.
OAuth data stays in ae-runtime's private home; nothing is logged to CI.
This only starts the local prompt and never calls a model or touches production.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
AUTH = Path('/run/ae-stage614-browser/Xauthority')
PYTHON = ROOT / 'owner-siwc-6-14/venv/bin/python'


def run(args: list[str], timeout: int = 18) -> bool:
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active AI Agent Engine release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer', 'ae614-xorg.service',
                 'ae614-fix-openbox.service', 'ae614-fix-firefox.service'):
        if not run(['systemctl', 'is-active', '--quiet', unit]):
            raise SystemExit('required unit inactive: ' + unit)
    if not (PYTHON.is_file() and AUTH.is_file() and
            Path('/tmp/.X11-unix/X14').is_socket()):
        raise SystemExit('owner GPT login runtime or display is missing')
    if run(['systemctl', 'is-active', '--quiet', 'ae614-gpt-login.service']):
        raise SystemExit('GPT login terminal is already open')
    if not run(['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
                '/usr/bin/test', '-r', str(AUTH)]):
        raise SystemExit('temporary Xauthority cannot be read by ae-runtime')
    if not run(['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
                '/usr/bin/env', 'HOME=/home/ae-runtime',
                str(PYTHON), '-I', '-m', 'ai_agent_engine.auth', 'status']):
        raise SystemExit('owner SIWC CLI status is not healthy')
    environment = {
        'HOME': '/home/ae-runtime',
        'USER': 'ae-runtime',
        'LOGNAME': 'ae-runtime',
        'DISPLAY': ':14',
        'XAUTHORITY': str(AUTH),
        'XDG_RUNTIME_DIR': '/run/ae-stage614-browser/xdg-runtime',
        'BROWSER': '/usr/bin/firefox-esr',
        'LANG': 'C.UTF-8',
        'PATH': '/home/ae-runtime/.local/bin:/usr/local/bin:/usr/bin:/bin',
    }
    args = [
        'systemd-run', '--unit=ae614-gpt-login',
        '--uid=ae-runtime', '--property=RuntimeMaxSec=720',
        '--property=NoNewPrivileges=yes',
    ]
    for key, value in environment.items():
        args.append('--setenv=' + key + '=' + value)
    args += [
        '--', '/usr/bin/xterm', '-hold',
        '-title', 'AI Agent Engine - ChatGPT authorization',
        '-fa', 'DejaVu Sans Mono', '-fs', '14',
        '-geometry', '103x26+30+45',
        '-bg', '#10141c', '-fg', '#f5f5f5',
        '-e', str(PYTHON), '-I', '-m', 'ai_agent_engine.auth', 'login',
    ]
    if not run(args):
        raise SystemExit('GPT owner login terminal could not start')
    time.sleep(3)
    if not run(['systemctl', 'is-active', '--quiet',
                'ae614-gpt-login.service']):
        raise SystemExit('GPT login terminal exited immediately')
    print('STAGE6_14_GPT_LOGIN ' + json.dumps({
        'status': 'INTERACTIVE_OWNER_LOGIN_STARTED_ON_VPS',
        'provider': 'chatgpt',
        'account': 'ae-runtime',
        'browser_on_callback_host': True,
        'login_timeout_seconds': 300,
        'terminal_max_seconds': 720,
        'authorization_completed': False,
        'tokens_exported': False,
        'models_invoked': False,
        'public_ports_opened': False,
        'production_release_unchanged': True,
        'services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
