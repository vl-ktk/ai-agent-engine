# AI Agent Engine — Stage 4.2 / 6.3

**One human operator:** Vlad is the only owner/user of AI Agent Engine. Do not implement registration, other human accounts, teams, or subscription tenants.

## What was verified on the actual Netcup VPS (2026-11-17)

- Debian 13.7; preflight confirmed `ae-runtime` absent, then owner-approved root console commands created it: UID/GID 988, home `/var/lib/ai-agent-engine`, shell `/usr/sbin/nologin`. `passwd -S` reports `L` (locked password). Existing `ae-deploy` is UID 1000.
- `/home/ae-deploy/ai-agent-engine-runtime` and its `releases` subdirectory are both `ae-deploy:ae-deploy`, mode `0700`.
- No `ae-*` systemd units before the change; `systemctl --failed` returned none before and after the identity setup.
- `journalctl --disk-usage` returned 24 MiB before the identity change. This is usage, **not** a configured size limit.
- After the live change: `/opt/ai-agent-engine` and `/opt/ai-agent-engine/releases` are `root:root` 0755; `/var/lib/ai-agent-engine` is `ae-runtime:ae-runtime` 0700. No service/SSH/journal/firewall changes.
- The repository's `python -m ai_agent_engine` currently runs a temporary-directory offline demo and exits. **There is no continuous server entrypoint or running controller to supervise.**

## Two Linux identities are not two human users

| Identity | Function | Interactive/login | Access |
| --- | --- | --- | --- |
| Vlad (owner) | Sole human administrator of AI Agent Engine | Owner-controlled interfaces only | Approved actions |
| `ae-deploy` | Stage release files and perform non-root SSH from approved GitHub workflow | Existing SSH key only | Own private staging tree; no sudo granted |
| `ae-runtime` (live) | Run an eventual daemon without shell or elevated rights | `/usr/sbin/nologin`; no SSH key/sudo | Runtime state only; no write permission to releases/code |

The operating-system accounts isolate execution privileges even with one human operator.

## Live identity, inactive service and logging configuration

1. **LIVE PASS after explicit owner approval:** equivalent guarded `useradd`/`install -d` commands were run directly through Netcup OOB root console, not by executing the repo shell script. `infra/bootstrap/prepare_runtime_identity.sh` remains the reviewed reproducible reference. No changes to `ae-deploy`, root password, sudoers, SSH, firewall or secrets.
2. `ae-controller.service.template` is a **disabled template**, not a unit installed on the VPS. The placeholder `@ENGINE_EXEC_START@` must be replaced with a verified, long-running runtime executable, after it actually exists. Never use the current offline `__main__.py` demo as a daemon.
3. The template restricts service privilege, filesystem writes and log message rate; persistent state and temporary runtime directories are managed by systemd with mode `0700`.
4. `journald-limit.conf.example` proposes 256 MiB persistent / 128 MiB runtime journal caps. It is **not** applied: this is a system-wide setting and would constrain logs of other services.
5. Monitoring currently uses verified read-only inventory: `systemctl --failed --no-pager`, `journalctl --disk-usage`. After a real daemon exists, require `systemctl is-active ae-controller` **plus an application health/heartbeat check** (process status alone does not prove the queue works). No monitoring alarms or health endpoints are running yet.

## Owner-approved execution, later

- Confirm current VPS identity and take a pre-change snapshot of service and account state.
- **Completed:** owner authorized creation of `ae-runtime` and protected directories. Verified `getent passwd`, `id`, `passwd -S`, `stat` modes/owners and empty failed-service list.
- Obtain separate approval before installing a systemd unit, changing journald limits or granting any new permissions. Only install/start a unit after real controller code exists and passes tests; verify new health endpoint, journals, restarts and rollback.
- If the user/group or path state is unexpected, **STOP** before modifying it. No automation has authority for these privileged operations.

**Current 6.3 status:** dedicated Linux runtime user and directory isolation **LIVE PASS**. Systemd daemon, global journal caps and application-level monitoring are **NOT activated**. Keep 6.3 OPEN until a real long-running controller exists and the remaining separately approved live steps are verified.

Reference: systemd.exec, systemd.service, journald.conf official manuals at https://www.freedesktop.org/software/systemd/man/latest/.
