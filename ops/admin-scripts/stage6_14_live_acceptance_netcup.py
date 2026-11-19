"""Run the owner's one approved Stage 6.14 GPT+Claude acceptance on Netcup.

This is a one-shot, owner-scoped call through the installed production-release
Python package and the unmodified provider CLI. It does not deploy, restart,
modify production data, read credentials, or print model/provider output.
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


def run(argv: list[str], *, timeout: int = 35) -> tuple[bool, bytes]:
    try:
        p = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        return p.returncode == 0, p.stdout[:15000]
    except (OSError, subprocess.TimeoutExpired):
        return False, b''


def production() -> None:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', EXPECTED):
        raise SystemExit('expected owner release identity missing')
    if os.readlink(ROOT / 'current') != 'releases/' + EXPECTED:
        raise SystemExit('production release changed')
    for unit in ('ae-controller.service', 'ae-backup.timer',
                 'ae-host-check.timer'):
        if not run(['systemctl', 'is-active', '--quiet', unit])[0]:
            raise SystemExit('production unit unhealthy')


def ensure_test_git() -> bool:
    """Install a missing Debian Git executable for disposable local repos.

    Never touches the production release or adds a Git remote. No update or
    upgrade occurs when an executable /usr/bin/git is already installed.
    """
    git = Path('/usr/bin/git')
    if git.is_file() and os.access(git, os.X_OK):
        if not run([str(git), '--version'], timeout=10)[0]:
            raise SystemExit('local test git is not runnable')
        return False
    apt_env = dict(os.environ)
    apt_env.update({'DEBIAN_FRONTEND': 'noninteractive',
                    'APT_LISTCHANGES_FRONTEND': 'none'})
    try:
        installed = subprocess.run([
            '/usr/bin/apt-get', 'install', '-y', '--no-install-recommends',
            'git'], check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=apt_env, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit('local test git provisioning unavailable') from None
    if (installed.returncode != 0 or not git.is_file()
            or not os.access(git, os.X_OK)
            or not run([str(git), '--version'], timeout=10)[0]):
        raise SystemExit('local test git provisioning did not pass')
    return True


def main() -> None:
    production()
    if not PYTHON.is_file() or not Path('/usr/local/bin/claude').is_file():
        raise SystemExit('owner provider executables missing')
    home = pwd.getpwnam(USER).pw_dir
    clean = ['/usr/bin/env', '-i', 'HOME=' + home, 'USER=' + USER,
             'LOGNAME=' + USER, 'LANG=C.UTF-8',
             'PATH=' + home + '/.local/bin:/usr/local/bin:/usr/bin:/bin']

    def owner(args: list[str], timeout: int = 35) -> tuple[bool, bytes]:
        return run(['/usr/sbin/runuser', '-u', USER, '--',
                    *clean, *args], timeout=timeout)

    # Check existing subscriptions without using their access tokens.
    gpt_ok, gpt_raw = owner([
        str(PYTHON), '-I', '-m', 'ai_agent_engine.auth', 'status'])
    gpt = gpt_ok and (b'Saved ChatGPT identity:' in gpt_raw
                       and b'ChatGPT plan permission: GRANTED' in gpt_raw
                       and b'Access token expired.' not in gpt_raw)
    claude_ok, claude_raw = owner(['claude', 'auth', 'status'])
    try:
        claude = json.loads(claude_raw) if claude_ok else None
    except (UnicodeError, ValueError):
        claude = None
    claude_ready = (isinstance(claude, dict)
                    and claude.get('loggedIn') is True
                    and claude.get('authMethod') == 'claude.ai'
                    and claude.get('apiProvider') in (None, 'firstParty')
                    and claude.get('subscriptionType') in ('pro', 'max'))
    if not (gpt and claude_ready):
        print('STAGE6_14_OWNER_E2E ' + json.dumps({
            'status': 'AUTH_PREFLIGHT_BLOCKED', 'gpt_ready': bool(gpt),
            'claude_subscription_cli_ready': bool(claude_ready),
            'model_calls': False, 'credentials_printed': False,
            'production_release_unchanged': True,
        }, sort_keys=True), flush=True)
        raise SystemExit('one or both subscription CLI checks failed')

    git_installed_now = ensure_test_git()
    production()

    # The same actual subprocess/SQLite/git code as live, using offline
    # deterministic workers. If this fails, NEVER send a model request.
    offline_ok, offline_out = owner([
        str(PYTHON), '-I', '-m', 'ai_agent_engine.task_stage6_14_acceptance',
        '--provider', 'both'], timeout=155)
    offline_pass = (offline_ok and b'STAGE6_14_OFFLINE PASS: gpt,claude' in offline_out)
    if not offline_pass:
        print('STAGE6_14_OWNER_E2E ' + json.dumps({
            'status': 'OFFLINE_BLOCKED', 'gpt_ready': True,
            'claude_subscription_cli_ready': True, 'offline_pass': False,
            'git_installed_now': git_installed_now,
            'model_calls': False, 'credentials_printed': False,
            'production_release_unchanged': True,
        }, sort_keys=True), flush=True)
        raise SystemExit('offline acceptance did not pass')

    # Recheck release immediately before the only two authorized live calls.
    production()
    live_ok, live_out = owner([
        str(PYTHON), '-I', '-m', 'ai_agent_engine.task_stage6_14_acceptance',
        '--live', '--owner-approved', '--provider', 'both'], timeout=490)
    live_pass = (live_ok and b'STAGE6_14_LIVE PASS: gpt,claude' in live_out)
    production()
    print('STAGE6_14_OWNER_E2E ' + json.dumps({
        'status': 'LIVE_PASS' if live_pass else 'LIVE_BLOCKED',
        'gpt_ready': True, 'claude_subscription_cli_ready': True,
        'offline_pass': True,
        'git_installed_now': git_installed_now,
        'gpt_claude_e2e_pass': live_pass,
        'live_request_attempted_once': True,
        'retry': False,
        'credentials_printed': False, 'model_responses_printed': False,
        'temporary_sqlite_git_artifacts_only': True,
        'production_release_unchanged': True,
        'production_services_healthy': True,
    }, sort_keys=True), flush=True)
    if not live_pass:
        raise SystemExit('one-shot live acceptance did not pass')


if __name__ == '__main__':
    main()
