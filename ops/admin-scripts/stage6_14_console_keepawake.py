"""Prevent kernel-console idle blanking during the owner-only VPS GUI session.

No persistent configuration file is changed. If supported, consoleblank is
set to zero in the running kernel, which resets on reboot. Then refresh VT7.
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
CONSOLEBLANK = Path('/sys/module/kernel/parameters/consoleblank')
AUTH = Path('/run/ae-stage614-browser/Xauthority')


def check(args: list[str], timeout: int = 12) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, ''
    return r.returncode == 0, r.stdout


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer', 'ae614-xorg.service',
                 'ae614-fix-openbox.service', 'ae614-fix-firefox.service'):
        if not check(['systemctl', 'is-active', '--quiet', unit])[0]:
            raise SystemExit('required production or private GUI unit is inactive')
    before = None
    after = None
    changed = False
    writable = None
    if CONSOLEBLANK.exists():
        raw = CONSOLEBLANK.read_text(encoding='ascii').strip()
        if not re.fullmatch(r'\d{1,6}', raw):
            raise SystemExit('unknown kernel consoleblank setting')
        before = int(raw)
        if before:
            try:
                CONSOLEBLANK.write_text('0\n', encoding='ascii')
                writable = True
            except OSError:
                writable = False
            changed = writable
        raw = CONSOLEBLANK.read_text(encoding='ascii').strip()
        if not re.fullmatch(r'\d{1,6}', raw):
            raise SystemExit('kernel consoleblank state cannot be verified')
        after = int(raw)
    if not AUTH.is_file():
        raise SystemExit('temporary Xauthority missing')
    ok, xstate = check([
        '/usr/sbin/runuser', '-u', 'ae-runtime', '--', '/usr/bin/env',
        'DISPLAY=:14', 'XAUTHORITY=' + str(AUTH), '/usr/bin/xset', 'q',
    ])
    if not ok:
        raise SystemExit('temporary X server cannot be queried')
    saver = re.search(r'\btimeout:\s*(\d+)\s+cycle:', xstate)
    x_saver_seconds = int(saver.group(1)) if saver else None
    if x_saver_seconds != 0:
        raise SystemExit('X screen blanking is unexpectedly active')
    _, vt_before = check(['/usr/bin/fgconsole'])
    if not check(['/usr/bin/chvt', '1'])[0]:
        raise SystemExit('cannot scan text console')
    time.sleep(2)
    if not check(['/usr/bin/chvt', '7'])[0]:
        raise SystemExit('cannot restore private Firefox screen')
    _, vt_after = check(['/usr/bin/fgconsole'])
    if vt_after.strip() != '7':
        raise SystemExit('console did not return to graphical VT7')
    print('STAGE6_14_KERNEL_CONSOLE ' + json.dumps({
        'status': 'CONSOLE_IDLE_CHECK_COMPLETE',
        'kernel_consoleblank_before': before,
        'kernel_consoleblank_after': after,
        'kernel_setting_changed': changed,
        'kernel_setting_writable': writable,
        'x_screensaver_timeout_seconds': x_saver_seconds,
        'console_before': vt_before.strip() if vt_before.strip().isdigit() else None,
        'console_after': 7,
        'private_display_refreshed': True,
        'persistent_os_configuration_changed': False,
        'production_release_unchanged': True,
        'credentials_accessed': False,
        'model_calls': False,
        'public_network_changes': False,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
