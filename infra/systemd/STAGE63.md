# AI Agent Engine — Stage 4.2 / 6.3

**One human operator:** Vlad is the only owner/user of AI Agent Engine. Do not implement registration, other human accounts, teams, or subscription tenants.

## What was verified on the actual Netcup VPS (2026-11-17)

- Debian 13.7; `ae-deploy` (UID 1000) is the only project-specific Linux identity; `ae-runtime` is absent.
- `/home/ae-deploy/ai-agent-engine-runtime` and its `releases` subdirectory are both `ae-deploy:ae-deploy`, mode `0700`.
- No `ae-*` systemd units; `systemctl --failed` returned none.
- `journalctl --disk-usage` returned 24 MiB. This is usage, **not** a configured size limit.
- The repository's `python -m ai_agent_engine` currently runs a temporary-directory offline demo and exits. **There is no continuous server entrypoint or running controller to supervise.**

## Two Linux identities are not two human users

| Identity | Function | Interactive/login | Access |
| --- | --- | --- | --- |
| Vlad (owner) | Sole human administrator of AI Agent Engine | Owner-controlled interfaces only | Approved actions |
| `ae-deploy` | Stage release files and perform non-root SSH from approved GitHub workflow | Existing SSH key only | Own private staging tree; no sudo granted |
| `ae-runtime` (planned) | Run an eventual daemon without shell or elevated rights | `/usr/sbin/nologin`; no SSH key/sudo | Runtime state only; no write permission to releases/code |

The operating-system accounts isolate execution privileges even with one human operator.

## Prepared, not activated

1. `infra/bootstrap/prepare_runtime_identity.sh` creates or verifies a dedicated no-login `ae-runtime` identity and private `/var/lib/ai-agent-engine`, with code/release directories owned by root. Run **only** after explicit owner approval of user and file-permission changes. It does not modify `ae-deploy`, root password, sudoers, SSH, firewall, or secrets.
2. `ae-controller.service.template` is a **disabled template**, not a unit installed on the VPS. The placeholder `@ENGINE_EXEC_START@` must be replaced with a verified, long-running runtime executable, after it actually exists. Never use the current offline `__main__.py` demo as a daemon.
3. The template restricts service privilege, filesystem writes and log message rate; persistent state and temporary runtime directories are managed by systemd with mode `0700`.
4. `journald-limit.conf.example` proposes 256 MiB persistent / 128 MiB runtime journal caps. It is **not** applied: this is a system-wide setting and would constrain logs of other services.
5. Monitoring currently uses verified read-only inventory: `systemctl --failed --no-pager`, `journalctl --disk-usage`. After a real daemon exists, require `systemctl is-active ae-controller` **plus an application health/heartbeat check** (process status alone does not prove the queue works). No monitoring alarms or health endpoints are running yet.

## Owner-approved execution, later

- Confirm current VPS identity and take a pre-change snapshot of service and account state.
- Obtain separate approval for creation of `ae-runtime` and ownership changes. Validate the guarded script; run it on Netcup OOB console, then check `getent passwd ae-runtime`, `id ae-runtime`, and directory ownership/modes.
- Obtain separate approval before installing a systemd unit, changing journald limits or granting any new permissions. Only install/start a unit after real controller code exists and passes tests; verify new health endpoint, journals, restarts and rollback.
- If the user/group or path state is unexpected, **STOP** before modifying it. No automation has authority for these privileged operations.

**Current 6.3 status:** design and safe files prepared, live account/service/logging changes **not performed**. Keep 6.3 OPEN until live execution and health verification are possible.

Reference: systemd.exec, systemd.service, journald.conf official manuals at https://www.freedesktop.org/software/systemd/man/latest/.
