"""Disable idle blanking only for the temporary Netcup console X display.

No system-wide display policy, model calls, credentials or deployed release
are touched. This may also wake a currently blanked virtual framebuffer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time


EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
ROOT = Path('/opt/ai-agent-engine')
AUTH = Path('/run/ae-stage614-browser/Xauthority')
USER = 'ae-runtime'


def run(args: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, ''
    return r.returncode == 0, r.stdout


def xset(*args: str) -> tuple[bool, str]:
    return run(['/usr/sbin/runuser', '-u', USER, '--', '/usr/bin/env',
                'DISPLAY=:14', 'XAUTHORITY=' + str(AUTH),
                '/usr/bin/xset', *args])


def timeout_from(info: str) -> int | None:
    m = re.search(r'\btimeout:\s*(\d+)\s+cycle:', info)
    return int(m.group(1)) if m else None


def baseline() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer', 'ae614-xorg.service',
                 'ae614-fix-openbox.service', 'ae614-fix-firefox.service'):
        if not run(['systemctl', 'is-active', '--quiet', unit])[0]:
            raise SystemExit('necessary unit inactive: ' + unit)
    if not (AUTH.is_file() and Path('/tmp/.X11-unix/X14').is_socket()):
        raise SystemExit('private X11 display missing')


def main() -> None:
    baseline()
    if not Path('/usr/bin/xset').is_file():
        raise SystemExit('xset utility not installed')
    ok, before = xset('q')
    if not ok:
        raise SystemExit('cannot query private X server screen saver')
    if not all(xset(*cmd)[0] for cmd in
               [('s', 'off'), ('s', 'noblank'), ('-dpms'), ('s', 'reset')]):
        raise SystemExit('could not disable temporary X server blanking')
    ok, after = xset('q')
    if not ok or timeout_from(after) != 0:
        raise SystemExit('temporary screen saver did not disable')
    # Reset VT scanout once to restore Netcup's remote framebuffer after blank.
    if not run(['/usr/bin/chvt', '1'])[0]:
        raise SystemExit('cannot switch to original text VT')
    time.sleep(2)
    if not run(['/usr/bin/chvt', '7'])[0]:
        raise SystemExit('cannot restore Firefox VT')
    baseline()
    print('STAGE6_14_SCREEN_BLANK ' + json.dumps({
        'status': 'PRIVATE_SCREEN_BLANKING_DISABLED',
        'prior_timeout_seconds': timeout_from(before),
        'new_timeout_seconds': timeout_from(after),
        'console_refreshed': True,
        'public_port_changes': False,
        'auth_attempted': False,
        'credentials_accessed': False,
        'model_calls': False,
        'production_release_unchanged': True,
        'services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
