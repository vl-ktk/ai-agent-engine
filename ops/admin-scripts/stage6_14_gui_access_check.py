"""Verify Xorg access for ae-runtime without reading or emitting cookies."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
RUNTIME = Path('/run/ae-stage614-browser')
AUTH = RUNTIME / 'Xauthority'
PROFILE = RUNTIME / 'firefox-profile'


def check(argv: list[str], timeout: int = 12) -> bool:
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def try_xterm(username: str | None) -> bool:
    envvars = [
        'DISPLAY=:14',
        'XAUTHORITY=' + str(AUTH),
        'HOME=/home/ae-runtime',
        'XDG_RUNTIME_DIR=' + str(RUNTIME / 'xdg-runtime'),
    ]
    cmd = ['/usr/bin/env', *envvars,
           '/usr/bin/xterm', '-display', ':14', '-e', '/usr/bin/true']
    if username:
        cmd = ['/usr/sbin/runuser', '-u', username, '--', *cmd]
    return check(cmd, timeout=9)


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    if not (RUNTIME.exists() and AUTH.is_file()
            and Path('/tmp/.X11-unix/X14').is_socket()):
        raise SystemExit('temporary X server was not found')
    results = {
        'status': 'ACCESS_PROBE_COMPLETE',
        'xauth_file_accessible_to_runtime': check(
            ['/usr/sbin/runuser', '-u', 'ae-runtime', '--',
             '/usr/bin/test', '-r', str(AUTH)]),
        'xterm_connect_as_runtime': try_xterm('ae-runtime'),
        'xterm_connect_as_root': try_xterm(None),
        'xorg_running': check(
            ['systemctl', 'is-active', '--quiet', 'ae614-xorg.service']),
        'services_healthy': all(
            check(['systemctl', 'is-active', '--quiet', u]) for u in
            ('ae-controller.service', 'ae-backup.timer',
             'ae-host-check.timer')),
        'browser_profile_present': PROFILE.is_dir(),
        'credentials_accessed': False,
        'model_calls': False,
        'production_changes': False,
    }
    print('STAGE6_14_X_ACCESS ' + json.dumps(results, sort_keys=True),
          flush=True)


if __name__ == '__main__':
    main()
