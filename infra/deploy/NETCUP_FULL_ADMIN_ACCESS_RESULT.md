# Netcup full administrative setup — LIVE PASS, 19 November 2026

All five owner-authorized points completed within the existing VPS/account.
No new server/MCP, paid API or new credential was created. Roadmap 6.14 was not
started and remains LIVE BLOCKED pending a separate owner instruction.

| Point | Result | Live evidence |
| --- | --- | --- |
| 1. Administrative entry and baseline | PASS | Existing CCP → SCP Auto-Login → existing root console; original release, SQLite integrity, 7 verified backups. Independent cloud baseline [44412865906](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44412865906). |
| 2. Full server control | PASS | root-owned 0440 sudoers rule grants ae-deploy full NOPASSWD sudo; visudo PASS; cloud SSH sudo id → uid 0; root/password SSH remain disabled. |
| 3. Control through connected GitHub | PASS | Owner request created through the connected GitHub tool, hosted Actions → existing pinned SSH identity → root job, results read through connected GitHub tools. [44416162976](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44416162976). |
| 4. Full live acceptance | PASS | Package install, transient service, checked wheel, deploy/restart/health, actual rollback/restart/health, final reactivation, SQLite/file inventory unchanged, backup verification PASS. Same successful run. |
| 5. Independence from Windows | PASS for the administrative execution channel | Request creation, execution, corrective commits/retries and result reading used cloud GitHub tools only after bootstrap. Runner Ubuntu 26.04/Python 3.12, VPS Debian 13/Python 3.13; no local file, local process, browser console or Windows SSH session was used by those jobs. Physical PC power-off was not performed. This is an on-demand cloud channel, not a newly created always-running GPT service. |

## Installed and rollback identities

- Final installed package: **0.3.11a0**.
- Final immutable VPS release: **0e04af007375d82daf8f7d3b36c71abb0c784e0b**.
- Previous working release retained for rollback:
  **85375103800845eeff696e53f510c66c428408ed**.
- Successful owner request commit: **141f9362617621324066af6ceca7500eda16b110**.
- Request ID: **20261119005500000000000000000005**.
- Wheel SHA-256: **bf5591cb326f238266b8cbe8d0d93324ac01830a2bdbad26efafca27dd3d4b89**.
- Original GitHub baseline before this task: **0c1c244f6bb2656c8d7c3bcafec981a2fb4a4294**.
- Revert source changes only with an addressed reverse commit on current main;
  never reset/force-push main or overwrite a concurrent release.

The package source and infrastructure fix belong to the installed release SHA.
Later report/status commits do not imply another VPS deployment.

## Actual VPS changes

1. Saved original sudoers/SSH configuration and original release pointer under
   private root directory `/root/ae-admin-20261119/`.
2. Installed `/etc/sudoers.d/90-ae-deploy-admin` as root:root 0440:
   `ae-deploy ALL=(ALL:ALL) NOPASSWD: ALL`. Full visudo validation PASS.
3. Existing SSH configuration/host identity were preserved. Effective
   `permitrootlogin no`, `passwordauthentication no`,
   `kbdinteractiveauthentication no` were checked LIVE.
4. Installed Debian repository test package `hello 2.10-5`; actual install
   occurred in [44415101527](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44415101527).
   Final acceptance confirmed it runs. No other package upgrades were requested.
5. Created private root job archives, online SQLite backups, ordinary-file
   snapshots and evidence under `/root/ae-admin-jobs/<request_id>/`.
   Evidence includes `evidence/result.json`, inventory reports and
   `evidence/data-before/projects/engine.sqlite3`.
   Existing seven retained backup snapshots were verified, not replaced.
6. Created new immutable release directories, switched only the managed
   `current` symlink and restarted only `ae-controller.service`. Backup and
   host-check timers stayed active. Transient smoke services succeeded and were
   collected. No working data was restored over production or deleted.

Final logical-data/file inventory before deploy, after deploy, after rollback
and after reactivation: one working SQLite file; SHA-256
`019e7190beed5cb1b471f14f530246bf59ea2a67c6c9e4108c8c34aac23efd68`.
SQLite integrity was checked in every inventory. Ordinary workspaces/artifacts
contained no additional working files. Final `backup verify PASS bytes=53834 retained=7`.

## Source and workflow validation

- Local focused validation before publishing: 23 security/workflow/request tests,
  compile, indexed-source secret scan and diff checks PASS.
- Final hosted preflight: **657 tests OK** on Python 3.12, before exposing SSH
  credentials to the execution step.
- [CI 44416162937](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44416162937):
  Linux Python **3.11/3.12/3.13 PASS**, isolated wheel/build/promotion PASS.
  The overall CI is red because of the existing Windows incompatibilities:
  unavailable `fcntl` and a symlink test. Do not describe the whole matrix as green.
- New regression uses a real synthetic wheel, real venv and pip under umask 077
  and checks staged file/directory permissions. The release stager explicitly
  normalizes verified release permissions, skipping external interpreter links.
- `netcup-owner-admin.yml` is the active full-admin owner channel;
  `netcup-gitops-status.yml` remains the active fixed read-only status channel.
  Other historical manual workflows were not re-certified by this task.
  The old separate signing-key activation workflow remains prepared/unused.

## Failures encountered and real recovery

- [44414795618](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44414795618):
  Ubuntu 24.04 preflight lacked the required sandbox behavior; stopped before
  SSH. Changed to the same tested Ubuntu 26.04/Python 3.12 environment as Linux CI.
- [44415101527](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44415101527):
  cloud root/package/service smoke succeeded; first release activation failed.
  The private root umask 077 left virtualenv directories at 0700 and systemd
  logged EXEC Permission denied. The scripted recovery actually returned to the
  original release and checked it healthy. No data change was observed: the
  next accepted baseline has the same inventory digest.
- [44415678275](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44415678275)
  and [44415867978](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44415867978):
  the new regression stopped before SSH, first on an external interpreter link,
  then on copied activation-template modes. Corrected the test's scope and
  normalized all actual staged files/directories.
- Final successful run independently logged the old failed stage's 0700 modes
  and historical EXEC failure, proved staged imports as ae-runtime before
  promotion, then completed the entire deploy → rollback → reactivation cycle.
  Failed staged release/evidence were retained; no irreversible cleanup occurred.

## Operation, boundaries and remaining issues

Follow [NETCUP_OWNER_ADMIN.md](./NETCUP_OWNER_ADMIN.md) for requests, logs,
locking, exact source/script hashes and revoking the sudo rule.
Jobs are restricted to the owner repository/main/push actor/rerun actor,
never PRs, command text or uncommitted message content. Every request selects
an already committed reviewed script; only that single JSON file may be added.
The existing deploy key is now root-equivalent by the owner's explicit choice.

The PC is not a runner, relay, key source or remote session dependency.
A connected cloud ChatGPT session can initiate this same GitHub channel.
This task did not create a permanently executing model, scheduler, or promise
that the current desktop conversation continues after its PC is switched off.

Remaining: existing Windows CI defects; no independently confirmed off-host
backup or total-VPS-loss restore. Node-20-targeting pinned GitHub Actions emit
a deprecation warning while executing on Node 24. These do not invalidate the
verified Linux/cloud administration, but must not be reported as resolved.
