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
