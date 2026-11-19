"""Quiet the temporary owner GUI during Claude Code's interactive OAuth.

No credentials, browser profile or provider response is read. This script
only controls owner-specific transient GUI units and the guest virtual tty;
the production controller, stored data and deployed release are untouched.
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
PRIVATE_UNITS = ('ae614-xorg.service', 'ae614-fix-openbox.service',
                 'ae614-fix-firefox.service')
CLAUDE_UNIT = 'ae614-claude-login.service'
REFRESH_TIMER = 'ae614-auto-vt-refresh.timer'
RETURN_TIMER = 'ae614-return-after-refresh.timer'


def call(argv: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
        return p.returncode == 0, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ''


def need(argv: list[str], purpose: str) -> str:
    ok, out = call(argv)
    if not ok:
        raise SystemExit('failed: ' + purpose)
    return out


def active(unit: str) -> bool:
    return call(['systemctl', 'is-active', '--quiet', unit])[0]


def production() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('expected production release missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if not active(unit):
            raise SystemExit('production unit is inactive')


def main() -> None:
    production()
    if not Path('/run/ae-stage614-browser/Xauthority').is_file():
        raise SystemExit('private display authorization missing')
    if not Path('/tmp/.X11-unix/X14').is_socket():
        raise SystemExit('private X11 socket missing')
    for unit in PRIVATE_UNITS:
        if not active(unit):
            raise SystemExit('private GUI expired: ' + unit)
    if not active(CLAUDE_UNIT):
        raise SystemExit('interactive Claude CLI expired')

    # Stop ONLY the 55s console refresh. It occasionally routed owner input
    # into the root text console during its brief VT1 portion.
    if active(REFRESH_TIMER):
        need(['systemctl', 'stop', REFRESH_TIMER], 'pause 55s VT refresh')
    if active(RETURN_TIMER):
        need(['systemctl', 'stop', RETURN_TIMER], 'pause old GUI return')

    # A fresh login instance resets the 25-minute transient xterm limit.
    # The previous one had not accepted any code (as seen in the owner GUI).
    need(['systemctl', 'restart', CLAUDE_UNIT], 'reopen Claude CLI login')
    time.sleep(3)
    if not active(CLAUDE_UNIT):
        raise SystemExit('new Claude login terminal was not retained')

    need(['/usr/bin/chvt', '1'], 'one-time screen scan')
    time.sleep(0.7)
    need(['/usr/bin/chvt', '7'], 'show private graphical console')
    if need(['/usr/bin/fgconsole'], 'check active virtual tty').strip() != '7':
        raise SystemExit('VT7 is not active')

    # Return to non-graphical console after 20 minutes, not during input.
    need(['systemd-run', '--unit=ae614-claude-quiet-return',
          '--on-active=1200s', '--timer-property=AccuracySec=1s', '--',
          '/usr/bin/chvt', '1'], 'schedule 20-minute return')
    production()
    print('STAGE6_14_CLAUDE_GUI_QUIET ' + json.dumps({
        'status': 'CLAUDE_LOGIN_TERMINAL_REOPENED',
        'temporary_gui_active': True,
        'guest_vt': 7,
        '55s_refresh_paused': not active(REFRESH_TIMER),
        'quiet_window_seconds': 1200,
        'previous_incomplete_login_restarted': True,
        'claude_owner_login_completed': False,
        'credentials_read': False,
        'model_calls': False,
        'production_release_unchanged': True,
        'production_services_healthy': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
