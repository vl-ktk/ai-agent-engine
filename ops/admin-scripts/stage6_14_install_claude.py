"""Owner-authorized Stage 6.14 Claude CLI preparation on existing Netcup.

Installs official Claude Code native CLI ONLY for the unprivileged ae-runtime user.
Does not authenticate accounts, inspect credentials, invoke a model, modify
services/SQLite, deploy a new AI Agent Engine release, or log private output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import subprocess

ROOT = Path('/opt/ai-agent-engine')
USER = 'ae-runtime'
EXPECTED = os.environ.get('AE_EXPECTED_CURRENT', '')
RELEASE_RE = re.compile(r'[0-9a-f]{40}\Z')
INSTALLER_URL = 'https://claude.ai/install.sh'


def child(argv: list[str], *, timeout: int = 20, input_bytes: bytes | None = None,
          env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    io = ({'stdin': subprocess.DEVNULL} if input_bytes is None
          else {'input': input_bytes})
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False, env=env, **io)


def assert_release() -> None:
    if not RELEASE_RE.fullmatch(EXPECTED):
        raise RuntimeError('expected release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise RuntimeError('active release changed; aborting')


def as_runtime(args: list[str], home: Path, *, timeout: int = 30,
               input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    env_args = ['/usr/bin/env', '-i', f'HOME={home}', f'USER={USER}',
                f'LOGNAME={USER}', 'LANG=C.UTF-8',
                f'PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin']
    return child(['/usr/sbin/runuser', '-u', USER, '--', *env_args, *args],
                 timeout=timeout, input_bytes=input_bytes)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit('requires authorized root administrative job')
    assert_release()
    health = child(['systemctl', 'is-active', '--quiet', 'ae-controller.service',
                    'ae-backup.timer', 'ae-host-check.timer'])
    if health.returncode:
        raise SystemExit('baseline server service health failed')
    account = pwd.getpwnam(USER)
    home = Path(account.pw_dir)
    runtime_bin = home / '.local/bin/claude'
    if not home.is_dir() or home.is_symlink():
        raise SystemExit('runtime home invalid')
    if as_runtime(['/usr/bin/test', '-w', str(home)], home).returncode != 0:
        raise SystemExit('runtime home not writable, no permissions changed')
    existing = as_runtime(['/usr/bin/test', '-x', str(runtime_bin)], home)
    installed_now = False
    if existing.returncode != 0:
        # Only the official Anthropic installer URL; fetched with system TLS.
        download = child(['/usr/bin/curl', '-fsSL', '--max-time', '60', INSTALLER_URL], timeout=70)
        installer = download.stdout
        if download.returncode or not 100 <= len(installer) <= 512_000 or not installer.startswith(b'#!'):
            raise SystemExit('official installer validation failed; no install executed')
        outcome = as_runtime(['/bin/bash', '-s'], home, timeout=240, input_bytes=installer)
        if outcome.returncode:
            raise SystemExit('official Claude CLI install failed; output withheld')
        installed_now = True
    # Use a stable root-managed PATH entry for systemd workers, without changing
    # working release, service files or the user's authentication material.
    if as_runtime(['/usr/bin/test', '-x', str(runtime_bin)], home).returncode:
        raise SystemExit('Claude CLI executable unavailable after install')
    global_bin = Path('/usr/local/bin/claude')
    if global_bin.is_symlink():
        if os.readlink(global_bin) != str(runtime_bin):
            raise SystemExit('global Claude entry belongs to another install')
    elif global_bin.exists():
        raise SystemExit('global Claude executable already exists; refused overwrite')
    else:
        global_bin.symlink_to(runtime_bin)
    if as_runtime(['/usr/local/bin/claude', '--version'], home, timeout=40).returncode:
        raise SystemExit('Claude CLI self-check failed; output withheld')
    assert_release()
    health = child(['systemctl', 'is-active', '--quiet', 'ae-controller.service',
                    'ae-backup.timer', 'ae-host-check.timer'])
    if health.returncode:
        raise SystemExit('final server service health failed')
    # No account identity, credential bytes, CLI output, email, or local paths.
    print('STAGE6_14_CLAUDE_PREP ' + json.dumps({
        'status': 'CLI_READY_FOR_OWNER_LOGIN', 'installed_now': installed_now,
        'claude_cli_usable': True, 'models_invoked': False,
        'gpt_auth_touched': False, 'claude_auth_touched': False,
        'release_unchanged': True, 'services_healthy': True}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
