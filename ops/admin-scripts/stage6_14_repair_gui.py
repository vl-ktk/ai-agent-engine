"""Repair temporary Xauthority ownership and start the private VPS browser.

Uses the existing Netcup Screen, without exposing ports, invoking models,
reading credentials or modifying the AI Agent Engine production release.
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
RUNTIME = Path('/run/ae-stage614-browser')
AUTH = RUNTIME / 'Xauthority'
DISPLAY = ':14'
USER = 'ae-runtime'


def run(argv: list[str], *, timeout: int = 15) -> bool:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def baseline() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed; no GUI repair allowed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if not run(['systemctl', 'is-active', '--quiet', unit]):
            raise SystemExit('production service health failed')


def xterm_connects() -> bool:
    return run([
        '/usr/sbin/runuser', '-u', USER, '--', '/usr/bin/env',
        'DISPLAY=' + DISPLAY, 'XAUTHORITY=' + str(AUTH),
        'HOME=/home/ae-runtime',
        'XDG_RUNTIME_DIR=' + str(RUNTIME / 'xdg-runtime'),
        '/usr/bin/xterm', '-display', DISPLAY, '-e', '/usr/bin/true',
    ], timeout=9)


def start_unit(unit: str, argv: list[str], env: dict[str, str]) -> bool:
    args = ['systemd-run', '--unit=' + unit,
            '--property=RuntimeMaxSec=3900',
            '--property=NoNewPrivileges=yes',
            '--uid=' + USER]
    for name, value in env.items():
        args.append('--setenv=' + name + '=' + value)
    return run([*args, '--', *argv])


def main() -> None:
    baseline()
    if not RUNTIME.is_dir() or not AUTH.is_file():
        raise SystemExit('temporary browser directory not found')
    if not Path('/tmp/.X11-unix/X14').is_socket():
        raise SystemExit('private Xorg socket not found')
    if not run(['systemctl', 'is-active', '--quiet',
                'ae614-xorg.service']):
        raise SystemExit('original private Xorg is no longer running')
    if any(run(['systemctl', 'is-active', '--quiet', unit]) for unit in
           ('ae614-fix-openbox.service', 'ae614-fix-firefox.service')):
        raise SystemExit('repaired browser already running; manual review needed')

    account = pwd.getpwnam(USER)
    prior_owner_correct = AUTH.stat().st_uid == account.pw_uid
    # xauth may atomically replace the cookie file with a root-owned file.
    # Fix only metadata: never inspect or emit the cookie itself.
    os.chown(AUTH, account.pw_uid, account.pw_gid)
    AUTH.chmod(0o600)
    if not xterm_connects():
        raise SystemExit('ae-runtime still cannot access the private X display')

    env = {
        'HOME': account.pw_dir, 'USER': USER, 'LOGNAME': USER,
        'DISPLAY': DISPLAY, 'XAUTHORITY': str(AUTH),
        'XDG_RUNTIME_DIR': str(RUNTIME / 'xdg-runtime'),
        'LANG': 'C.UTF-8',
    }
    if not start_unit('ae614-fix-openbox', ['/usr/bin/openbox'], env):
        raise SystemExit('repaired Openbox transient start failed')
    if not start_unit('ae614-fix-firefox', [
        '/usr/bin/dbus-run-session', '--', '/usr/bin/firefox-esr',
        '--no-remote', '--new-instance', '--profile',
        str(RUNTIME / 'firefox-profile'), 'about:blank'], env):
        raise SystemExit('repaired Firefox transient start failed')
    time.sleep(4)
    for unit in ('ae614-xorg.service', 'ae614-fix-openbox.service',
                 'ae614-fix-firefox.service'):
        if not run(['systemctl', 'is-active', '--quiet', unit]):
            raise SystemExit('private graphical process exited: ' + unit)

    # Return the guest console to tty1 even if the owner leaves the page open.
    if not run(['systemd-run', '--unit=ae614-return-console',
                '--on-active=3600s', '--', '/usr/bin/chvt', '1']):
        raise SystemExit('could not set the automatic console return')
    if not run(['/usr/bin/chvt', '7']):
        raise SystemExit('could not show the temporary browser on Netcup Screen')
    baseline()
    print('STAGE6_14_OWNER_GUI ' + json.dumps({
        'status': 'PRIVATE_FIREFOX_ON_NETCUP_SCREEN',
        'display_vt': 7,
        'xauth_ownership_repaired': not prior_owner_correct,
        'owner_user': USER,
        'firefox_active': True, 'window_manager_active': True,
        'automatic_console_return_seconds': 3600,
        'public_port_opened': False, 'models_invoked': False,
        'owner_login_performed': False, 'credentials_accessed': False,
        'production_release_unchanged': True, 'services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
