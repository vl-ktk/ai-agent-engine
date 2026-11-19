"""Try a safe video mode refresh for Netcup SCP Screen's black console.

The temporary Firefox display exists and serves windows inside Xorg. Only
the transient console mode/VT may change; no production service is restarted.
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


def run(argv: list[str], *, timeout: int = 18,
        env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=timeout,
            env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ''
    return r.returncode == 0, r.stdout


def baseline() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[0-9a-f]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active production release changed')
    for service in ('ae-controller.service', 'ae-backup.timer',
                    'ae-host-check.timer', 'ae614-xorg.service',
                    'ae614-fix-openbox.service', 'ae614-fix-firefox.service'):
        if not run(['systemctl', 'is-active', '--quiet', service])[0]:
            raise SystemExit('required service inactive: ' + service)


def xrandr(args: list[str]) -> tuple[bool, str]:
    env = {
        'HOME': '/home/ae-runtime',
        'LANG': 'C.UTF-8',
        'DISPLAY': ':14',
        'XAUTHORITY': str(AUTH),
        'XDG_RUNTIME_DIR': '/run/ae-stage614-browser/xdg-runtime',
        'PATH': '/usr/bin:/bin',
    }
    return run(['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
                '/usr/bin/xrandr', *args], env=env)


def current_size(raw: str) -> str:
    found = re.search(r'^Screen \d+:.*current (\d+) x (\d+)', raw, re.M)
    if not found:
        return 'unknown'
    return found.group(1) + 'x' + found.group(2)


def main() -> None:
    baseline()
    installed_now = False
    if not Path('/usr/bin/xrandr').is_file():
        env = dict(os.environ)
        env.update({'DEBIAN_FRONTEND': 'noninteractive',
                    'APT_LISTCHANGES_FRONTEND': 'none'})
        if not run(['apt-get', 'install', '-y', '--no-install-recommends',
                    'x11-xserver-utils'], timeout=180, env=env)[0]:
            raise SystemExit('xrandr utility install failed')
        installed_now = True
    if not Path('/usr/bin/xrandr').is_file():
        raise SystemExit('xrandr missing')

    ok, before = xrandr(['--current'])
    if not ok:
        raise SystemExit('ae-runtime cannot query temporary X display')
    old_size = current_size(before)
    mode_available = bool(re.search(r'^\s+1024x768\s', before, re.M))
    mode_changed = False
    if mode_available and old_size != '1024x768':
        mode_changed = xrandr(['--size', '1024x768'])[0]
    ok, after = xrandr(['--current'])
    if not ok:
        raise SystemExit('temporary X display was lost after mode check')

    # Re-scan the guest's VGA display after the mode switch.
    if not run(['/usr/bin/chvt', '1'])[0]:
        raise SystemExit('could not temporarily select text console')
    time.sleep(2)
    if not run(['/usr/bin/chvt', '7'])[0]:
        raise SystemExit('could not restore the Firefox console')
    baseline()
    print('STAGE6_14_CONSOLE_VIDEO ' + json.dumps({
        'status': 'VIDEO_MODE_REFRESH_ATTEMPTED',
        'old_size': old_size,
        'new_size': current_size(after),
        'requested_1024x768_mode_available': mode_available,
        'mode_changed': mode_changed,
        'forced_guest_console_rescan': True,
        'xrandr_installed_now': installed_now,
        'public_network_changes': False,
        'authentication_attempted': False,
        'model_calls': False,
        'production_release_unchanged': True,
        'services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
