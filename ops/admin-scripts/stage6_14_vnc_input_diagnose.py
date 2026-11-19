"""Read-only probe for real keyboard/mouse drivers on private Netcup Xorg."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')


def run(args: list[str]) -> tuple[bool, str]:
    try:
        p = subprocess.run(args, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=20, check=False)
        return p.returncode == 0, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ''


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer', 'ae614-xorg.service'):
        if not run(['systemctl', 'is-active', '--quiet', unit])[0]:
            raise SystemExit('service inactive')
    drivers = {}
    for package in ('xserver-xorg-input-libinput', 'xserver-xorg-input-evdev',
                    'xinput', 'libinput10'):
        ok, out = run(['dpkg-query', '-W', '-f=${Status}', package])
        drivers[package] = ok and out.strip() == 'install ok installed'
    ok, journal = run(['journalctl', '-u', 'ae614-xorg.service',
                       '-b', '--no-pager', '-o', 'cat', '-n', '3500'])
    if not ok:
        journal = ''
    log = Path('/var/log/Xorg.14.log')
    if log.is_file():
        journal += '\n' + log.read_text(errors='replace')[-150000:]
    ok, xinputs = run(['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
                       '/usr/bin/env', 'DISPLAY=:14',
                       'XAUTHORITY=/run/ae-stage614-browser/Xauthority',
                       '/usr/bin/xinput', 'list', '--short'])
    real_x_devices = []
    for line in xinputs.splitlines():
        if 'id=' in line and 'Virtual core' not in line and 'XTEST' not in line:
            real_x_devices.append(line.strip().split('id=')[0].strip())
    print('STAGE6_14_VNC_INPUT ' + json.dumps({
        'status': 'READ_ONLY_INPUT_DIAGNOSED',
        'package_installed': drivers,
        'xorg_input_driver_files': {
            'libinput': Path('/usr/lib/xorg/modules/input/libinput_drv.so').exists(),
            'evdev': Path('/usr/lib/xorg/modules/input/evdev_drv.so').exists(),
        },
        'guest_input_event_device_count': len(list(Path('/dev/input').glob('event*'))),
        'xorg_log_has_libinput_driver_loaded':
            bool(re.search(r"Using input driver ['\"]libinput", journal)),
        'xorg_log_mentions_no_input_driver': 'No input driver specified' in journal,
        'xorg_log_mentions_failed_input_module':
            bool(re.search(r'Failed to load module ["\'](?:libinput|evdev)', journal)),
        'xinput_query_worked': ok,
        'xinput_non_synthetic_names': real_x_devices[:10],
        'authentication_performed': False,
        'credentials_accessed': False,
        'model_calls': False,
        'production_release_unchanged': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
