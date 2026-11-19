"""Fix the temporary owner's GPT login terminal to use the real OS home.

No browser profile or OAuth material is inspected or exported. A failed private
login xterm is replaced with a working login prompt on the existing display.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
AUTH = Path('/run/ae-stage614-browser/Xauthority')
PYTHON = ROOT / 'owner-siwc-6-14/venv/bin/python'
USER = 'ae-runtime'


def run(args: list[str], timeout: int = 25) -> bool:
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              check=False, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[0-9a-f]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer', 'ae614-xorg.service',
                 'ae614-fix-openbox.service', 'ae614-fix-firefox.service',
                 'ae614-auto-vt-refresh.timer'):
        if not run(['systemctl', 'is-active', '--quiet', unit]):
            raise SystemExit('required unit not active: ' + unit)
    if not (PYTHON.is_file() and AUTH.is_file() and
            Path('/tmp/.X11-unix/X14').is_socket()):
        raise SystemExit('owner login runtime not available')

    home = Path(pwd.getpwnam(USER).pw_dir)
    if not (home.is_dir() and run(['/usr/sbin/runuser', '-u', USER,
                                  '--', '/usr/bin/test', '-w', str(home)])):
        raise SystemExit('real OS user home is not writable')
    if not run(['/usr/sbin/runuser', '-u', USER, '--',
                '/usr/bin/test', '-r', str(AUTH)]):
        raise SystemExit('owner cannot read private display authority')
    if not run(['/usr/sbin/runuser', '-u', USER, '--',
                '/usr/bin/env', 'HOME=' + str(home),
                str(PYTHON), '-I', '-m', 'ai_agent_engine.auth', 'status']):
        raise SystemExit('owner GPT auth CLI status failed')

    if run(['systemctl', 'is-active', '--quiet',
            'ae614-gpt-login-hour.service']):
        if not run(['systemctl', 'stop', 'ae614-gpt-login-hour.service']):
            raise SystemExit('cannot close failed private owner login terminal')

    login_py = (
        'from ai_agent_engine.siwc import AuthError, login\n'
        'try:\n'
        '    result = login(timeout=3000)\n'
        'except AuthError as exc:\n'
        "    print('Login unavailable:', exc)\n"
        'else:\n'
        "    print('Identity verified. Plan permission:', 'GRANTED' "
        "if result['sharing_enabled'] else 'NOT GRANTED')\n"
    )
    cmd = [
        'systemd-run', '--unit=ae614-gpt-login-corrected',
        '--uid=' + USER, '--property=RuntimeMaxSec=3200',
        '--property=NoNewPrivileges=yes',
        '--setenv=HOME=' + str(home),
        '--setenv=USER=' + USER, '--setenv=LOGNAME=' + USER,
        '--setenv=DISPLAY=:14', '--setenv=XAUTHORITY=' + str(AUTH),
        '--setenv=XDG_RUNTIME_DIR=/run/ae-stage614-browser/xdg-runtime',
        '--setenv=BROWSER=/usr/bin/firefox-esr',
        '--setenv=LANG=C.UTF-8',
        '--setenv=PATH=' + str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin',
        '--', '/usr/bin/xterm', '-hold',
        '-title', 'AI Agent Engine - ChatGPT authorization',
        '-fa', 'DejaVu Sans Mono', '-fs', '14',
        '-geometry', '103x26+30+45', '-bg', '#10141c', '-fg', '#f5f5f5',
        '-e', str(PYTHON), '-I', '-c', login_py,
    ]
    if not run(cmd):
        raise SystemExit('corrected GPT login terminal failed to start')
    time.sleep(3)
    if not run(['systemctl', 'is-active', '--quiet',
                'ae614-gpt-login-corrected.service']):
        raise SystemExit('corrected owner login terminal exited')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed during login preparation')
    print('STAGE6_14_GPT_LOGIN_HOME ' + json.dumps({
        'status': 'PRIVATE_GPT_OWNER_LOGIN_REOPENED',
        'real_home_from_passwd': True,
        'auth_prompt_running': True,
        'login_timeout_seconds': 3000,
        'owner_login_performed': False,
        'models_invoked': False,
        'credentials_accessed': False,
        'public_ports_opened': False,
        'production_release_unchanged': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
