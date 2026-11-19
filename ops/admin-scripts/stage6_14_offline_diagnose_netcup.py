"""Locate a Stage 6.14 offline-only acceptance failure on Netcup.

No models, OAuth tokens, provider output, prompts or exception messages are
printed. The isolated test uses only disposable files and local subprocesses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import subprocess


ROOT = Path('/opt/ai-agent-engine')
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
PYTHON = ROOT / 'owner-siwc-6-14/venv/bin/python'
USER = 'ae-runtime'


OFFLINE_CODE = '''
import json, traceback
from pathlib import Path
from ai_agent_engine.task_stage6_14_acceptance import run_acceptance
try:
    result = run_acceptance(live=False, providers=('gpt','claude'))
    evidence = {'offline_pass': len(result) == 2}
except BaseException as exc:
    frames = traceback.extract_tb(exc.__traceback__)[-5:]
    evidence = {
        'offline_pass': False,
        'exception_class': type(exc).__name__[:70],
        'frames': [
            {'module': Path(f.filename).name[:80],
             'function': f.name[:80], 'line': f.lineno}
            for f in frames
        ],
    }
    if type(exc).__name__ == 'AcceptanceRejected' and exc.args:
        reason = exc.args[0]
        if reason in ('local_git_unavailable', 'local_git_failed',
                      'invalid_git_baseline', 'rollback_probe_not_created',
                      'unexpected_version_check', 'source_change_not_detected',
                      'rollback_failed', 'rollback_not_verified',
                      'provider_task_not_succeeded',
                      'audit_or_delivery_incomplete',
                      'artifact_transfer_failed',
                      'source_version_unverified',
                      'durable_recovery_unverified'):
            evidence['category'] = reason
print('STAGE6_14_OFFLINE_DIAG ' + json.dumps(evidence, sort_keys=True))
'''


def main() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('expected owner release missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed')
    if not PYTHON.is_file():
        raise SystemExit('owner venv is not available')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                          check=False, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=12).returncode:
            raise SystemExit('production service is unhealthy')
    home = pwd.getpwnam(USER).pw_dir
    argv = ['/usr/sbin/runuser', '-u', USER, '--', '/usr/bin/env', '-i',
            'HOME=' + home, 'USER=' + USER, 'LOGNAME=' + USER,
            'LANG=C.UTF-8',
            'PATH=' + home + '/.local/bin:/usr/local/bin:/usr/bin:/bin',
            str(PYTHON), '-I', '-c', OFFLINE_CODE]
    try:
        child = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit('offline diagnostic could not complete') from None
    text = child.stdout.decode('utf-8', errors='replace')
    marker = next((line for line in text.splitlines()
                   if line.startswith('STAGE6_14_OFFLINE_DIAG ')), '')
    if child.returncode or not marker:
        raise SystemExit('offline diagnostic marker missing')
    # OFFLINE_CODE emits only hard-coded categories, safe Python function
    # identifiers and file basenames. Never forward any other child output.
    print(marker, flush=True)
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed')


if __name__ == '__main__':
    main()
