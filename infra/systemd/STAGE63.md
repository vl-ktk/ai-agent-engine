# AI Agent Engine — Stage 4.2 / 6.3

**Only human operator:** Vlad. The Linux accounts `ae-deploy` and `ae-runtime` are technical process identities, **not** other people or engine tenants.

## Real Netcup VPS, 2026-11-17

Host: `v2202611460959561466`, Debian 13.7; one existing Netcup VPS. No extra VPS, GPU server, services from other projects, purchases, new ports or secrets.

### Runtime identity and filesystem — LIVE PASS

- Created `ae-runtime`, UID/GID 988, sole group `ae-runtime`; password locked (`passwd -S` = `L`); shell `/usr/sbin/nologin`, home `/var/lib/ai-agent-engine`. Verified `getent passwd` and `id`.
- `/var/lib/ai-agent-engine`: `ae-runtime:ae-runtime` mode `0700`.
- `/opt/ai-agent-engine` and `/opt/ai-agent-engine/releases`: `root:root` mode `0755` (runtime cannot write deployed code).
- Existing `ae-deploy` UID 1000 and its private 0700 deployment staging directories are unchanged.
- The reproducible, guarded reference is `infra/bootstrap/prepare_runtime_identity.sh`. Actual live commands were performed via the Netcup out-of-band root console; the reference script was **not** run.

### System journal retention — LIVE PASS

- **Applied on the VPS:** `/etc/systemd/journald.conf.d/90-ai-agent-engine-limit.conf`; source copy: [`infra/systemd/90-ai-agent-engine-limit.conf`](./90-ai-agent-engine-limit.conf).
- `SystemMaxUse=256M`, `RuntimeMaxUse=128M`. These are **system-wide journald limits**, affecting logs of other services too, not just the engine.
- `systemd-analyze cat-config systemd/journald.conf` confirmed both effective settings; `systemctl restart systemd-journald` completed; `systemctl is-active systemd-journald ssh` returned `active` for each. `journalctl --disk-usage` stayed 24 MiB (no vacuum/truncation).
- Drop-in verified `root:root` mode `0644`. VPS log partition `/dev/vda4`: 157G size / 1.1G used / 149G available before change.
- `journald-limit.conf.example` is a historical example, not an additional active setting; the `90-*.conf` file above records what was installed.

### Systemd host health monitoring — LIVE PASS

- **Installed:** `/etc/systemd/system/ae-host-check.service` and `/etc/systemd/system/ae-host-check.timer`, tracked at [`ae-host-check.service`](./ae-host-check.service) and [`ae-host-check.timer`](./ae-host-check.timer).
- The one-shot service runs **as `ae-runtime`** with `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`; checks the active state of `ssh.service`, `systemd-journald.service`, and `systemd-timesyncd.service`. Any failed `ExecStart` makes the unit fail and records the failure in the system journal.
- The timer is `enabled` and `active`: first check after boot, then ~every 15 minutes via `OnUnitInactiveSec=15min`. Verified a next scheduled run via `systemctl list-timers --all`.
- `systemd-analyze verify` for both unit files returned exit code 0; manual `systemctl start ae-host-check.service` completed, `Result=success`, `ExecMainStatus=0`, `User=ae-runtime`. `systemctl --failed --no-pager` reported **0 loaded failed units** afterward.
- `systemctl daemon-reload` and `systemctl enable --now ae-host-check.timer` emitted a nonfatal `systemd-ssh-generator: Failed to query local AF_VSOCK CID: Cannot assign requested address` warning on this VPS. Unit validation, the actual one-shot and timer succeeded. Do not mistake this warning for an engine failure.
- **Scope:** local host-service checks and journal evidence, **not** external alerts, controller health, queue throughput or application heartbeats. Those depend on a real daemon and later monitoring work.

### Controller service — PREPARED, NOT RUNNING

- `infra/systemd/ae-controller.service.template` is a **source template only**. It has `@ENGINE_EXEC_START@` and is **not installed/enabled** on the VPS.
- `python -m ai_agent_engine` still executes a temporary-directory offline demonstration and exits; **there is no verified long-running controller entrypoint**. Starting a fake or demo systemd daemon would be incorrect. The eventual controller requires a real executable, tested application health check and an owner-approved release.
- Therefore the **host infrastructure portion of 6.3 is LIVE PASS, but the complete item remains OPEN** solely for the real controller/app-level checks. Do not claim AI Agent Engine is live.

## Changes and rollback boundary

- No changes to Netcup firewall, SSH policy, root/deploy password, sudoers, access keys, secrets or application data; no reboot, log deletion, or production app deployment.
- Reference previous GitHub main `7f2569b620eaf9adf038628acaee13f19738c22f` for rollback of documentation/config definitions.
- If rollback of these **live** settings is separately approved, stop/disable the host timer, remove its two installed units and reload systemd; remove the journald drop-in and restart journald; verify active SSH and system journal. Do not delete `ae-runtime` or its private data during routine rollback.
- No automatic production deploy from CI; prepare application runtime only after the real controller exists and is verified.

Official references: [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html), [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html), [journald.conf](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html).
