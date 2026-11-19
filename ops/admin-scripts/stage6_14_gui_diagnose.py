"""Read-only diagnosis of the private Netcup VPS console browser launch.

Never prints journal text, environment variables, authorization cookies,
browser data or account credentials.
"""

import json
import os
from pathlib import Path
import re
import subprocess


EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')


def command(args: list[str], timeout: int = 12) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, text=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, ''
    return result.returncode == 0, result.stdout


def inspect(unit: str) -> dict[str, str]:
    ok, output = command(['systemctl', 'show', unit, '--no-pager',
                          '-p', 'ActiveState', '-p', 'SubState',
                          '-p', 'Result', '-p', 'ExecMainCode',
                          '-p', 'ExecMainStatus'])
    if not ok:
        return {'status': 'not_queryable'}
    data = dict(line.split('=', 1) for line in output.splitlines()
                if '=' in line)
    return {key: data.get(key, 'unknown') for key in
            ('ActiveState', 'SubState', 'Result',
             'ExecMainCode', 'ExecMainStatus')}


def errors(unit: str) -> dict[str, bool]:
    ok, output = command(['journalctl', '--no-pager', '--output=cat',
                          '-n', '80', '--unit', unit])
    low = output.lower() if ok else ''
    return {
        'display_access_error': any(s in low for s in
                                    ('cannot open display', 'failed to open display',
                                     'unable to open display', 'no protocol specified')),
        'dbus_error': 'dbus' in low and ('fail' in low or 'error' in low),
        'x11_error': any(s in low for s in ('x error', 'xconnection', 'x11 error')),
        'permission_error': 'permission denied' in low,
        'configuration_error': 'configuration' in low and 'error' in low,
        'bus_connection_error': 'failed to connect to bus' in low,
    }


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch(r'[a-f0-9]{40}', EXPECTED):
        raise SystemExit('owner release identity missing')
    if os.readlink('/opt/ai-agent-engine/current') != 'releases/' + EXPECTED:
        raise SystemExit('active release changed')
    services = ('ae-controller.service', 'ae-backup.timer', 'ae-host-check.timer')
    result = {
        'status': 'READ_ONLY_GUI_DIAGNOSIS',
        'x_socket_exists': Path('/tmp/.X11-unix/X14').exists(),
        'private_runtime_exists': Path('/run/ae-stage614-browser').exists(),
        'xorg': inspect('ae614-xorg.service'),
        'openbox': inspect('ae614-openbox.service'),
        'firefox': inspect('ae614-firefox.service'),
        'openbox_error_types': errors('ae614-openbox.service'),
        'firefox_error_types': errors('ae614-firefox.service'),
        'baseline_healthy': all(command(['systemctl', 'is-active', '--quiet', s])[0]
                                for s in services),
        'model_calls': False,
        'credentials_accessed': False,
        'production_changes': False,
    }
    print('STAGE6_14_GUI_DIAGNOSTIC ' + json.dumps(result, sort_keys=True),
          flush=True)


if __name__ == '__main__':
    main()
