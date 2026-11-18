# Stage 4.17 — root-managed read-only secret store

**Purpose:** allow the existing Netcup Debian 13 runtime account `ae-runtime` to read only explicitly requested, administrator-installed credentials. Keep secrets out of the public GitHub repository, stdout/stderr, systemd environment variables, command-line arguments, application traces and published artifacts.

## Ownership contract

| Path | Owner / group | Permissions | Access |
| --- | --- | --- | --- |
| `/etc/ai-agent-engine` | `root:root` | `0755` | Non-secret parent only |
| `/etc/ai-agent-engine/secrets` | `root:ae-runtime` | `0750` | Only root can create/modify, `ae-runtime` may read/list |
| Any future named credential | `root:ae-runtime` | `0440` | Runtime group read-only, nobody except root can alter |

- **No credentials are installed by stage 4.17.** The directory starts empty. Do not migrate pre-existing OAuth sessions, rotate keys or change GitHub Actions secrets as part of this step.
- The running controller stays on its existing release, since it does not read secrets yet. The source-level reader is `ai_agent_engine/secret_store.py`, only for trusted runtime code; **do not expose** its `read()` method as an agent tool, HTTP endpoint, MCP endpoint or debug output.
- The reader opens an exact ASCII single-component name relative to an already opened directory, refusing symlinks, non-regular entries, wrong owner/group/mode, empty or oversized files and unsafe parents. No arbitrary root/path from a model request.
- If the directory or its permissions are unexpected, **stop**. Do not automatically `chmod`/`chown` an existing unknown path, and do not overwrite existing credentials.

## One-time empty directory provisioning (root console)

First inspect paths *read-only*; only create if absent, and verify ownership/mode before continuing:

```bash
ls -ld /etc /etc/ai-agent-engine /etc/ai-agent-engine/secrets
install -d -o root -g root -m 0755 /etc/ai-agent-engine
install -d -o root -g ae-runtime -m 0750 /etc/ai-agent-engine/secrets
stat -c '%U:%G %a %n' /etc/ai-agent-engine /etc/ai-agent-engine/secrets
runuser -u ae-runtime -- test -r /etc/ai-agent-engine/secrets
runuser -u ae-deploy -- ls /etc/ai-agent-engine/secrets  # expected: Permission denied
```

Run `python3 -m ai_agent_engine.secret_store audit` **only as `ae-runtime`** from the exact version-locked runtime source when installed; `audit` reports file count and PASS/FAIL, but never credential names or contents. Running `python3 -m ai_agent_engine.secret_store smoke` verifies a synthetic throwaway file under the system temp directory, and never reads or changes real secrets. Do not run newly changed code in production without an integrity check against its GitHub release.

## Future secret installation and rotation (requires owner authorization)

- No install/write/export CLI is intentionally provided. A host administrator must use a protected, non-logging input channel, root-only staging, an atomic replacement method, `root:ae-runtime / 0440` and independent post-install validation; never put secret bytes in shell arguments/history, source, CI variables echoed to logs, or GitHub artifact archives.
- A compromised process **already running as `ae-runtime` can read credentials granted to that Unix account**. This step isolates the deployment account, *not* individual agents; later stages must establish per-agent grants and a broker if isolation between agents is required.
- Files are protected by Unix permissions, **not encrypted at rest**. `systemd.exec` additionally supports `LoadCredential=` / `LoadCredentialEncrypted=` for service credential delivery; introducing these or migrating real secrets requires separate planned work and owner consent.
- Backups of credentials (if ever needed) require encrypted handling and a decision outside 4.17. Stage 4.18 concerns backups for the task DB and files; do not silently include this private directory.

## Safety and rollback

- Test only artificial bytes. Keep `ae-controller.service` untouched; service is already protected by `ProtectSystem=strict`, `User=ae-runtime`, `Group=ae-runtime` and `UMask=0077`.
- A repository rollback does **not** remove a secret directory or revoke real secrets; GitHub code rollback is a compatible new commit. Do not delete any credential file as a routine rollback action. An empty store can remain ready for the next stage; removal only by owner decision after checking emptiness.
- Verification must record: source/CI version, actual directory stats, runtime access, `ae-deploy` denial, service health, absence of real secrets/paid operations, and known gaps.

## Verified live result — 18 November 2026

- Netcup VPS `v2202611460959561466`: both paths were absent before provisioning. Created **only empty directories**, using root console after owner approval.
- `ls -ld` confirmed `/etc/ai-agent-engine` as `root:root` / `0755` (`drwxr-xr-x`) and `/etc/ai-agent-engine/secrets` as `root:ae-runtime` / `0750` (`drwxr-x---`).
- `runuser -u ae-runtime -- ls -a /etc/ai-agent-engine/secrets` returned only `.` and `..`: readable and **empty**. `runuser -u ae-deploy -- ls /etc/ai-agent-engine/secrets` returned `Permission denied`.
- `systemctl is-active ae-controller.service` returned `active` **after** provisioning. No service restart/release promote; the installed controller remains `0.3.8a0` (`85375103800845eeff696e53f510c66c428408ed`).
- Source-only credential reader version `0.3.10a0` committed as `23ff633a7eec5c7dfadf91ea67ac0b60bf9284f5`; local 234 unit tests PASS (1 skipped), synthetic smoke PASS, wheel build PASS, public-source secret scan PASS, [offline CI #44259072118](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44259072118) SUCCESS. Reader **not installed** into the existing controller release: do not imply a live secret-read integration; runtime/OS access was checked live, file semantics were verified on synthetic test data.
- Nothing sensitive was entered, moved, printed, backed up, added to GitHub or rotated. A future administrator must authorize, install and validate each real credential separately. No new paid services.
