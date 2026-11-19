"""Install missing Xorg libinput and reconnect Netcup SCP Screen controls.

Only the private, transient Stage 6.14 X11 processes are restarted. The
existing production controller, persistent authentication and SQLite data
are untouched. Never reads browser secrets or initiates model requests.
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
GRAPHICS = ('ae614-xorg.service', 'ae614-fix-openbox.service',
            'ae614-fix-firefox.service')
OWNER_LOGIN = 'ae614-gpt-login-corrected.service'


def run(args: list[str], timeout: int = 30,
        env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout, env=env)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ''


def need(args: list[str], description: str) -> str:
    ok, output = run(args)
    if not ok:
        raise SystemExit('failed: ' + description)
    return output


def active(unit: str) -> bool:
    return run(['systemctl', 'is-active', '--quiet', unit])[0]


def production() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if not active(unit):
            raise SystemExit('production health failed')


def owner_x(*args: str) -> tuple[bool, str]:
    return run(['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
                '/usr/bin/env', 'DISPLAY=:14', 'XAUTHORITY=' + str(AUTH),
                *args])


def main() -> None:
    production()
    if not AUTH.is_file():
        raise SystemExit('temporary Xauthority not found')
    for unit in (*GRAPHICS, OWNER_LOGIN, 'ae614-auto-vt-refresh.timer'):
        if not active(unit):
            raise SystemExit('temporary owner session has expired: ' + unit)

    input_driver = Path('/usr/lib/xorg/modules/input/libinput_drv.so')
    was_missing = not input_driver.is_file()
    xinput_missing = not Path('/usr/bin/xinput').is_file()
    if was_missing or xinput_missing:
        env = dict(os.environ)
        env.update({'DEBIAN_FRONTEND': 'noninteractive',
                    'APT_LISTCHANGES_FRONTEND': 'none'})
        ok, _ = run(['apt-get', 'install', '-y', '--no-install-recommends',
                     'xserver-xorg-input-libinput', 'xinput'],
                    timeout=170, env=env)
        if not ok:
            raise SystemExit('Debian X11 input driver installation failed')
    if not (input_driver.is_file() and Path('/usr/bin/xinput').is_file()):
        raise SystemExit('X11 input driver remains unavailable')

    # Xorg discovers newly installed hotplug input driver only on restart.
    # The owner's unfinished OAuth browser prompt must be restarted too.
    for unit in GRAPHICS:
        need(['systemctl', 'restart', unit], unit + ' restart')
        if unit == GRAPHICS[0]:
            for _ in range(25):
                if Path('/tmp/.X11-unix/X14').is_socket():
                    break
                time.sleep(.5)
            else:
                raise SystemExit('X11 socket did not return')
    time.sleep(4)
    for unit in GRAPHICS:
        if not active(unit):
            raise SystemExit('private GUI exited after driver install')

    for xcmd in (('/usr/bin/xset', 's', 'off'),
                 ('/usr/bin/xset', 's', 'noblank'),
                 ('/usr/bin/xset', '-dpms')):
        if not owner_x(*xcmd)[0]:
            raise SystemExit('cannot disable idle screen blanking')
    ok, geometry = owner_x('/usr/bin/xrandr', '--current')
    if not ok or '1024x768' not in geometry:
        raise SystemExit('Netcup VNC display resolution not available')
    if not owner_x('/usr/bin/xrandr', '--size', '1024x768')[0]:
        raise SystemExit('cannot restore remote VNC screen resolution')

    # Evidence must include real non-XTEST guest devices (not just the
    # artificial X11 pointer used by xdotool).
    ok, devices = owner_x('/usr/bin/xinput', 'list', '--short')
    if not ok:
        raise SystemExit('X11 input devices cannot be queried')
    physical = [line for line in devices.splitlines()
                if 'id=' in line and 'Virtual core' not in line
                and 'XTEST' not in line]
    if not physical:
        raise SystemExit('Xorg still has no physical keyboard or mouse')

    need(['systemctl', 'restart', OWNER_LOGIN], 'restart owner GPT prompt')
    time.sleep(4)
    if not active(OWNER_LOGIN):
        raise SystemExit('corrected owner GPT prompt failed to reopen')
    need(['/usr/bin/chvt', '1'], 'guest VNC rescan')
    time.sleep(1)
    need(['/usr/bin/chvt', '7'], 'show private browser')
    if need(['/usr/bin/fgconsole'], 'active console').strip() != '7':
        raise SystemExit('private browser VT not visible')
    production()
    print('STAGE6_14_VNC_INPUT_REPAIR ' + json.dumps({
        'status': 'XORG_HARDWARE_INPUT_ENABLED',
        'libinput_driver_installed_now': was_missing,
        'xinput_tool_installed_now': xinput_missing,
        'real_x11_input_device_count': len(physical),
        'private_gui_and_login_restarted': True,
        'owner_login_performed': False,
        'credentials_accessed': False,
        'models_invoked': False,
        'public_ports_changed': False,
        'production_release_unchanged': True,
        'production_services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
