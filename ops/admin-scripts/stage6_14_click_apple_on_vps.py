"""Click the owner's chosen Apple sign-in button in the private VPS Firefox.

The Netcup noVNC viewer can lose its input while continuing to display video.
Inject one pointer click locally with XTEST, guarded by the Firefox window
title. This never supplies credentials, accepts permission, or invokes models.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
AUTH = Path('/run/ae-stage614-browser/Xauthority')
USER = 'ae-runtime'


def run(argv: list[str], timeout: int = 25,
        env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        r = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, env=env, timeout=timeout)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ''


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('expected owner release missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer', 'ae614-xorg.service',
                 'ae614-fix-openbox.service', 'ae614-fix-firefox.service',
                 'ae614-auto-vt-refresh.timer',
                 'ae614-gpt-login-corrected.service'):
        if not run(['systemctl', 'is-active', '--quiet', unit])[0]:
            raise SystemExit('required private or production unit inactive')
    if not AUTH.is_file():
        raise SystemExit('private display authority missing')

    installed = False
    if not Path('/usr/bin/xdotool').is_file():
        env = dict(os.environ)
        env.update(DEBIAN_FRONTEND='noninteractive',
                   APT_LISTCHANGES_FRONTEND='none')
        if not run(['apt-get', 'install', '-y', '--no-install-recommends',
                    'xdotool'], timeout=160, env=env)[0]:
            raise SystemExit('cannot provide local X pointer utility')
        installed = True

    home = pwd.getpwnam(USER).pw_dir
    env_args = ['HOME=' + home, 'DISPLAY=:14', 'XAUTHORITY=' + str(AUTH)]

    def x(*args: str) -> tuple[bool, str]:
        return run(['/usr/sbin/runuser', '-u', USER, '--',
                    '/usr/bin/env', *env_args, '/usr/bin/xdotool', *args])

    # Stop if the browser no longer shows the owner's observed OpenAI page.
    ok, win = x('getactivewindow')
    if not ok or not win.strip().isdigit():
        raise SystemExit('no active private X11 window')
    ok, name = x('getwindowname', win.strip())
    if not ok or 'Welcome back - OpenAI' not in name:
        raise SystemExit('OpenAI login is not the foreground window')
    ok, screen = run(['/usr/sbin/runuser', '-u', USER, '--',
                      '/usr/bin/env', *env_args, '/usr/bin/xrandr',
                      '--current'])
    if not ok or 'current 1024 x 768' not in screen:
        raise SystemExit('observed 1024x768 login geometry changed')

    # The observed Apple button occupies approx. x=340..670, y=610..655
    # in the 1024x768 guest. Click once at its centre.
    if not x('mousemove', '--sync', '500', '632')[0]:
        raise SystemExit('could not move private X pointer')
    if not x('click', '--clearmodifiers', '1')[0]:
        raise SystemExit('Apple sign-in click was rejected by X11')
    time.sleep(5)
    ok, after_window = x('getactivewindow')
    title_changed = False
    if ok and after_window.strip().isdigit():
        ok2, after_title = x('getwindowname', after_window.strip())
        title_changed = bool(ok2 and after_title != name)
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('release changed during private GUI interaction')
    print('STAGE6_14_APPLE_CLICK ' + json.dumps({
        'status': 'PRIVATE_APPLE_BUTTON_CLICK_SENT',
        'local_x11_pointer_injected': True,
        'view_only_browser_input_not_required': True,
        'apple_page_transition_observed_by_window_title': title_changed,
        'xdotool_installed_now': installed,
        'password_or_code_entered': False,
        'credentials_accessed': False,
        'model_calls': False,
        'production_release_unchanged': True,
    }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
