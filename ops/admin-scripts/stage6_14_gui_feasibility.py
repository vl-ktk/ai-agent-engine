"""Read-only hardware/package feasibility for temporary owner OAuth GUI.

This probes no credentials, networking, logs, account IDs or model endpoints;
it does not start a display, install software or change production services.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')


def available_mem_mb() -> int:
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return -1
    return -1


def installed(name: str) -> bool:
    done = subprocess.run(
        ['dpkg-query', '-W', '-f=${Status}', name], check=False,
        capture_output=True, text=True, timeout=10,
    )
    return done.returncode == 0 and done.stdout.strip() == 'install ok installed'


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('requires owner admin exact release')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed')
    if subprocess.run(
        ['systemctl', 'is-active', '--quiet', 'ae-controller.service',
         'ae-backup.timer', 'ae-host-check.timer'], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
    ).returncode:
        raise SystemExit('services unhealthy')
    mem_mb = available_mem_mb()
    disk_mb = shutil.disk_usage(ROOT).free // (1024 * 1024)
    console_device = any(Path(n).exists() for n in
                         ('/dev/dri/card0', '/dev/fb0', '/sys/class/graphics/fb0'))
    packages = {n: installed(n) for n in
                ('xserver-xorg-core', 'xinit', 'openbox', 'firefox-esr', 'chromium')}
    active_gui = subprocess.run(
        ['pgrep', '-f', '^/usr/lib/xorg/Xorg|^/usr/bin/Xorg'],
        check=False, capture_output=True, timeout=10,
    ).returncode == 0
    print('STAGE6_14_GUI_FEASIBILITY ' + json.dumps({
        'mem_available_mb': mem_mb, 'disk_free_mb': disk_mb,
        'graphics_device_present': console_device,
        'gui_running': active_gui, 'packages_installed': packages,
        'min_resources_ok': mem_mb >= 1024 and disk_mb >= 1800,
        'models_invoked': False, 'credentials_accessed': False,
        'production_changes': False, 'release_unchanged': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
