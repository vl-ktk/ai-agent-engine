"""Launch official Claude Code sign-in in the owner's private VPS desktop.

The owner personally signs in via Anthropic's browser OAuth. No API key,
tokens, account identifiers or model responses are printed or copied to CI.
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
USER = 'ae-runtime'
AUTH = Path('/run/ae-stage614-browser/Xauthority')
SIWC = ROOT / 'owner-siwc-6-14/venv/bin/python'


def run(args: list[str], timeout: int = 25) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
        return r.returncode == 0, r.stdout[:1500]
    except (OSError, subprocess.TimeoutExpired):
        return False, ''


def need(args: list[str], label: str) -> str:
    ok, out = run(args)
    if not ok:
        raise SystemExit('failed: ' + label)
    return out


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer', 'ae614-xorg.service',
                 'ae614-fix-openbox.service', 'ae614-fix-firefox.service',
                 'ae614-auto-vt-refresh.timer'):
        need(['systemctl', 'is-active', '--quiet', unit], 'required unit')
    if not (AUTH.is_file() and Path('/tmp/.X11-unix/X14').is_socket()
            and SIWC.is_file()):
        raise SystemExit('private owner browser or GPT venv unavailable')
    if run(['systemctl', 'is-active', '--quiet',
            'ae614-claude-login.service'])[0]:
        raise SystemExit('Claude sign-in terminal already running')

    home = Path(pwd.getpwnam(USER).pw_dir)
    if not home.is_dir() or not run(['/usr/sbin/runuser', '-u', USER,
                                    '--', '/usr/bin/test', '-w', str(home)])[0]:
        raise SystemExit('owner OS home is not writable')
    clean_env = ['/usr/bin/env', '-i', 'HOME=' + str(home), 'USER=' + USER,
                 'LOGNAME=' + USER, 'LANG=C.UTF-8',
                 'PATH=' + str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin']
    gpt_ok, status = run(['/usr/sbin/runuser', '-u', USER, '--', *clean_env,
                          str(SIWC), '-I', '-m', 'ai_agent_engine.auth', 'status'])
    if not (gpt_ok and 'Saved ChatGPT identity:' in status and
            'ChatGPT plan permission: GRANTED' in status and
            'Access token expired.' not in status):
        raise SystemExit('GPT account is not ready; Claude login not started')

    claude_ok, _ = run(['/usr/sbin/runuser', '-u', USER, '--', *clean_env,
                        '/usr/local/bin/claude', '--version'], timeout=35)
    if not claude_ok:
        raise SystemExit('official Claude Code CLI not available')

    xenv = {
        'HOME': str(home), 'USER': USER, 'LOGNAME': USER,
        'DISPLAY': ':14', 'XAUTHORITY': str(AUTH),
        'XDG_RUNTIME_DIR': '/run/ae-stage614-browser/xdg-runtime',
        'BROWSER': '/usr/bin/firefox-esr', 'LANG': 'C.UTF-8',
        'PATH': str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin',
    }
    cmd = ['systemd-run', '--unit=ae614-claude-login',
           '--uid=' + USER, '--property=RuntimeMaxSec=1500',
           '--property=NoNewPrivileges=yes']
    for key, value in xenv.items():
        cmd.append('--setenv=' + key + '=' + value)
    cmd += ['--', '/usr/bin/xterm', '-hold',
            '-title', 'AI Agent Engine - Claude authorization',
            '-fa', 'DejaVu Sans Mono', '-fs', '14',
            '-geometry', '100x26+50+55',
            '-bg', '#10141c', '-fg', '#f5f5f5',
            '-e', '/usr/local/bin/claude', 'auth', 'login']
    need(cmd, 'launch Claude Code owner OAuth terminal')
    time.sleep(4)
    need(['systemctl', 'is-active', '--quiet',
          'ae614-claude-login.service'], 'Claude OAuth terminal remains open')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed during startup')
    print('STAGE6_14_CLAUDE_OWNER_LOGIN ' + json.dumps({
        'status': 'CLAUDE_OAUTH_TERMINAL_OPENED_ON_VPS',
        'gpt_previously_ready': True,
        'claude_cli_usable': True,
        'official_claude_auth_login_started': True,
        'owner_login_completed': False,
        'private_browser_display': ':14',
        'terminal_max_seconds': 1500,
        'credentials_printed_to_ci': False,
        'models_invoked': False,
        'public_ports_changed': False,
        'production_release_unchanged': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
