"""Install a minimal temporary console browser for owner SIWC on Netcup.

This prepares Xorg, Openbox and Firefox for the existing Netcup SCP Screen.
It never opens a network listener, runs OAuth, invokes models or starts a GUI.
The active AI Agent Engine release, services and database are not deployed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
PACKAGES = (
    'xserver-xorg-core', 'xinit', 'openbox', 'firefox-esr',
    'xauth', 'xterm', 'dbus-x11', 'fonts-dejavu-core',
)


def run(command: list[str], *, timeout: int, env: dict[str, str] | None = None) -> bool:
    try:
        result = subprocess.run(
            command, check=False, timeout=timeout, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def health() -> None:
    if not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('expected deployed release missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('deployed release changed, refusing GUI changes')
    if not run(['systemctl', 'is-active', '--quiet', 'ae-controller.service',
                'ae-backup.timer', 'ae-host-check.timer'], timeout=15):
        raise SystemExit('baseline service health failed')


def installed(package: str) -> bool:
    try:
        result = subprocess.run(
            ['dpkg-query', '-W', '-f=${Status}', package], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == 'install ok installed'


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit('requires owner admin access')
    health()
    if not Path('/dev/tty1').exists():
        raise SystemExit('guest VNC console TTY missing')
    already_ready = all(installed(name) for name in PACKAGES)
    if not already_ready:
        env = dict(os.environ)
        env.update({'DEBIAN_FRONTEND': 'noninteractive',
                    'APT_LISTCHANGES_FRONTEND': 'none',
                    'NEEDRESTART_MODE': 'l'})
        if not run(['apt-get', 'update', '-qq'], timeout=240, env=env):
            raise SystemExit('Debian package metadata update failed; details withheld')
        if not run(['apt-get', 'install', '-y', '--no-install-recommends',
                    *PACKAGES], timeout=420, env=env):
            raise SystemExit('GUI package installation failed; details withheld')
    if not all(installed(name) for name in PACKAGES):
        raise SystemExit('GUI packages missing after installation')
    if not all(Path(path).is_file() for path in
               ('/usr/bin/firefox-esr', '/usr/bin/openbox',
                '/usr/bin/xinit', '/usr/bin/xauth', '/usr/bin/xterm')):
        raise SystemExit('installed graphical commands missing')
    if not (Path('/usr/lib/xorg/Xorg').is_file()
            or Path('/usr/bin/Xorg').is_file()):
        raise SystemExit('Xorg server binary missing')
    health()
    print('STAGE6_14_GUI_PACKAGES ' + json.dumps({
        'status': 'GUI_PACKAGES_READY', 'installed_now': not already_ready,
        'xorg_ready': True, 'firefox_ready': True,
        'public_ports_opened': False, 'gui_session_started': False,
        'models_invoked': False, 'authentication_attempted': False,
        'credentials_accessed': False, 'release_unchanged': True,
        'services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
