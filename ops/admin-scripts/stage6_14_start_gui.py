"""Start a time-limited private Firefox desktop on the existing VPS console.

The owner views it only through netcup's existing authenticated SCP Screen.
No public service, new account, SSH access, token read or model call.
Display :14 runs on guest VT7, leaving the normal tty1 getty untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
import time


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
USER = 'ae-runtime'
RUNTIME = Path('/run/ae-stage614-browser')
DISPLAY = ':14'
XSOCKET = Path('/tmp/.X11-unix/X14')
TIMEOUT_SECONDS = 4500


def run(argv: list[str], *, timeout: int = 20,
        env: dict[str, str] | None = None) -> bool:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def baseline() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[0-9a-f]{40}', EXPECTED):
        raise SystemExit('expected root release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if not run(['systemctl', 'is-active', '--quiet', unit]):
            raise SystemExit('service health failed: ' + unit)


def transient(unit: str, argv: list[str], *, user: str | None = None,
              env: dict[str, str] | None = None) -> bool:
    args = ['systemd-run', '--unit=' + unit,
            '--property=RuntimeMaxSec=' + str(TIMEOUT_SECONDS),
            '--property=NoNewPrivileges=yes']
    if user is not None:
        args += ['--uid=' + user]
    for key, value in (env or {}).items():
        args.append('--setenv=' + key + '=' + value)
    return run([*args, '--', *argv])


def main() -> None:
    baseline()
    if XSOCKET.exists() or RUNTIME.exists() or any(
        run(['systemctl', 'is-active', '--quiet', 'ae614-' + unit + '.service'])
        for unit in ('xorg', 'openbox', 'firefox')):
        raise SystemExit('an existing graphical session needs manual review')
    if not Path('/dev/tty7').exists():
        raise SystemExit('unused graphical virtual terminal not available')
    account = pwd.getpwnam(USER)
    home = account.pw_dir
    RUNTIME.mkdir(mode=0o755)
    RUNTIME.chmod(0o755)
    authority = RUNTIME / 'Xauthority'
    authority.touch(mode=0o600)
    os.chown(authority, account.pw_uid, account.pw_gid)
    profile = RUNTIME / 'firefox-profile'
    profile.mkdir(mode=0o700)
    os.chown(profile, account.pw_uid, account.pw_gid)
    xdg = RUNTIME / 'xdg-runtime'
    xdg.mkdir(mode=0o700)
    os.chown(xdg, account.pw_uid, account.pw_gid)
    if not run(['/usr/bin/xauth', '-f', str(authority), 'add', DISPLAY, '.',
                secrets.token_hex(16)]):
        raise SystemExit('private X authentication setup failed')
    xorg = '/usr/lib/xorg/Xorg' if Path('/usr/lib/xorg/Xorg').is_file() else '/usr/bin/Xorg'
    if not transient('ae614-xorg', [xorg, DISPLAY, 'vt7', '-nolisten', 'tcp',
                                    '-auth', str(authority), '-noreset']):
        raise SystemExit('transient Xorg launch refused')
    for _ in range(12):
        if XSOCKET.exists() and run(['systemctl', 'is-active', '--quiet',
                                     'ae614-xorg.service']):
            break
        time.sleep(1)
    else:
        raise SystemExit('Xorg did not become ready; no browser launched')
    environment = {
        'HOME': home, 'USER': USER, 'LOGNAME': USER,
        'DISPLAY': DISPLAY, 'XAUTHORITY': str(authority),
        'XDG_RUNTIME_DIR': str(xdg), 'LANG': 'C.UTF-8',
    }
    if not transient('ae614-openbox', ['/usr/bin/openbox'],
                     user=USER, env=environment):
        raise SystemExit('window manager failed to launch')
    if not transient('ae614-firefox',
                     ['/usr/bin/dbus-run-session', '--', '/usr/bin/firefox-esr',
                      '--no-remote', '--new-instance', '--profile', str(profile),
                      'about:blank'], user=USER, env=environment):
        raise SystemExit('private Firefox session failed to start')
    time.sleep(3)
    for unit in ('ae614-xorg.service', 'ae614-openbox.service',
                 'ae614-firefox.service'):
        if not run(['systemctl', 'is-active', '--quiet', unit]):
            raise SystemExit('temporary graphical unit exited: ' + unit)
    # Restore console automatically before the graphical units time out.
    if not run(['systemd-run', '--unit=ae614-return-console',
                '--on-active=' + str(TIMEOUT_SECONDS - 120) + 's',
                '--', '/usr/bin/chvt', '1']):
        raise SystemExit('automatic console restoration could not be scheduled')
    # SCP Screen displays the current guest VT. No additional network listener.
    if not run(['/usr/bin/chvt', '7']):
        raise SystemExit('cannot switch Netcup VNC screen to temporary desktop')
    baseline()
    print('STAGE6_14_OWNER_GUI ' + json.dumps({
        'status': 'PRIVATE_BROWSER_START_REQUESTED',
        'vnc_console_vt': 7, 'firefox_unit_started': True,
        'owner_user': USER, 'max_session_seconds': TIMEOUT_SECONDS,
        'opened_public_port': False, 'auth_attempted': False,
        'models_invoked': False, 'release_unchanged': True,
        'services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
