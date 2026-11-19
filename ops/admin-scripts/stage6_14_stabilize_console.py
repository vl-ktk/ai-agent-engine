"""Recover the owner-only Netcup SCP Screen browser and refresh guest VTs.

The graphical units are transient, bound to an already-reviewed private
localhost OAuth workflow. This never reads browser profiles or credentials,
opens a network listener, calls an AI model, or changes production services.
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
TEMP_UNITS = (
    'ae614-xorg.service',
    'ae614-fix-openbox.service',
    'ae614-fix-firefox.service',
)


def command(args: list[str], timeout: int = 35) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ''


def required(args: list[str], *, description: str = 'command') -> str:
    ok, out = command(args)
    if not ok:
        raise SystemExit(description + ' failed')
    return out


def active(unit: str) -> bool:
    return command(['systemctl', 'is-active', '--quiet', unit])[0]


def production() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if not active(unit):
            raise SystemExit('production health check failed')


def owner_x(args: list[str]) -> None:
    required(['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
              '/usr/bin/env', 'DISPLAY=:14', 'XAUTHORITY=' + str(AUTH),
              *args], description='private X display')


def main() -> None:
    production()
    if not AUTH.is_file() or not Path('/tmp/.X11-unix/X14').is_socket():
        raise SystemExit('private graphical session no longer exists')
    for unit in TEMP_UNITS:
        if not active(unit):
            raise SystemExit('private graphical unit inactive: ' + unit)

    # The old auto-return is approaching and must not undo the new live window.
    if active('ae614-return-console.timer'):
        required(['systemctl', 'stop', 'ae614-return-console.timer'],
                 description='previous console expiry timer')

    # Restart ONLY our three transient GUI services, not production services.
    # A restart resets systemd's transient RuntimeMaxSec countdown.
    for unit in TEMP_UNITS:
        required(['systemctl', 'restart', unit], description='restart ' + unit)
        if unit == 'ae614-xorg.service':
            for _ in range(20):
                if Path('/tmp/.X11-unix/X14').is_socket():
                    break
                time.sleep(.5)
            else:
                raise SystemExit('private Xorg socket not restored')
    time.sleep(4)
    for unit in TEMP_UNITS:
        if not active(unit):
            raise SystemExit('private graphical unit exited after restart')
    owner_x(['/usr/bin/xset', 's', 'off'])
    owner_x(['/usr/bin/xset', 's', 'noblank'])
    owner_x(['/usr/bin/xset', '-dpms'])
    current = required(['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
                        '/usr/bin/env', 'DISPLAY=:14',
                        'XAUTHORITY=' + str(AUTH), '/usr/bin/xrandr',
                        '--current'], description='X resolution')
    if '1024x768' in current:
        owner_x(['/usr/bin/xrandr', '--size', '1024x768'])

    # Launch a fresh local owner-login prompt after the graphical restart.
    # Its sole outgoing request is the user's browser-driven OAuth login.
    if active('ae614-gpt-login-long.service'):
        required(['systemctl', 'stop', 'ae614-gpt-login-long.service'],
                 description='retire obsolete temporary login prompt')
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
    login_args = [
        'systemd-run', '--unit=ae614-gpt-login-hour',
        '--uid=ae-runtime', '--property=RuntimeMaxSec=3200',
        '--property=NoNewPrivileges=yes',
        '--setenv=HOME=/home/ae-runtime', '--setenv=USER=ae-runtime',
        '--setenv=LOGNAME=ae-runtime', '--setenv=DISPLAY=:14',
        '--setenv=XAUTHORITY=' + str(AUTH),
        '--setenv=XDG_RUNTIME_DIR=/run/ae-stage614-browser/xdg-runtime',
        '--setenv=BROWSER=/usr/bin/firefox-esr',
        '--setenv=LANG=C.UTF-8',
        '--setenv=PATH=/home/ae-runtime/.local/bin:/usr/local/bin:/usr/bin:/bin',
        '--', '/usr/bin/xterm', '-hold',
        '-title', 'AI Agent Engine - ChatGPT authorization',
        '-fa', 'DejaVu Sans Mono', '-fs', '14',
        '-geometry', '103x26+30+45', '-bg', '#10141c', '-fg', '#f5f5f5',
        '-e', str(ROOT / 'owner-siwc-6-14/venv/bin/python'),
        '-I', '-c', login_py,
    ]
    required(login_args, description='start temporary ChatGPT login prompt')
    time.sleep(3)
    if not active('ae614-gpt-login-hour.service'):
        raise SystemExit('temporary login prompt failed')

    # A short guest VT rescan restores Netcup VNC's stale black framebuffer.
    # Run it at most once per 55 sec during this temporary owner login hour.
    refresh_code = (
        'import subprocess,time\n'
        "subprocess.run(['/usr/bin/chvt','1'],check=True,timeout=8)\n"
        'time.sleep(0.7)\n'
        "subprocess.run(['/usr/bin/chvt','7'],check=True,timeout=8)\n"
    )
    required(['systemd-run', '--unit=ae614-auto-vt-refresh',
              '--on-active=55s', '--on-unit-active=55s',
              '--timer-property=AccuracySec=1s', '--',
              '/usr/bin/python3', '-c', refresh_code],
             description='temporary VT refresh timer')

    # Restore VT1 and stop refreshing after one hour. The three GUI services
    # retain their original bounded RuntimeMaxSec and expire by themselves.
    return_code = (
        'import subprocess\n'
        "subprocess.run(['systemctl','stop','ae614-auto-vt-refresh.timer'],check=True,timeout=15)\n"
        "subprocess.run(['/usr/bin/chvt','1'],check=True,timeout=8)\n"
    )
    required(['systemd-run', '--unit=ae614-return-after-refresh',
              '--on-active=3300s', '--', '/usr/bin/python3', '-c',
              return_code], description='final console return timer')

    required(['/usr/bin/chvt', '1'], description='guest video scan')
    time.sleep(1)
    required(['/usr/bin/chvt', '7'], description='restore private graphical VT')
    if required(['/usr/bin/fgconsole'], description='active guest VT').strip() != '7':
        raise SystemExit('private VT7 was not restored')
    production()
    print('STAGE6_14_STABLE_SCREEN ' + json.dumps({
        'status': 'TEMPORARY_GUI_RESTARTED_WITH_VIDEO_REFRESH',
        'guest_vt': 7,
        'auto_refresh_interval_seconds': 55,
        'auto_return_after_seconds': 3300,
        'gpt_login_prompt_restarted': True,
        'credentials_accessed': False,
        'owner_login_performed': False,
        'model_calls': False,
        'public_ports_changed': False,
        'production_release_unchanged': True,
        'production_services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
