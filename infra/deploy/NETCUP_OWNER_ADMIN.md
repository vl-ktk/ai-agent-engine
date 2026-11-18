# Existing Netcup owner administrative channel

Owner authorization: 19 November 2026, five-point full administrative setup.
This supersedes the earlier restricted-sudo proposal for this task. The existing
`ae-deploy` account now has `ALL=(ALL:ALL) NOPASSWD: ALL`; this is equivalent to
root for holders of its existing SSH key. Direct root SSH remains disabled.
No new MCP, server, signing secret, paid API or local SSH key is required.

## Bootstrap evidence

The existing CCP session successfully entered SCP through Auto-Login. The
existing root console confirmed the exact previous release
`85375103800845eeff696e53f510c66c428408ed`, active controller/timers, SQLite
`integrity_check=ok` and backup verification PASS with seven retained snapshots.
`ae-deploy` initially had no sudo. Before installation, root saved sudoers and
SSH configuration to `/root/ae-admin-20261119/`. The new rule was checked with
`visudo -cf`, installed root:root 0440 as
`/etc/sudoers.d/90-ae-deploy-admin`, and the full configuration passed `visudo -c`.
`runuser -u ae-deploy -- sudo -n id` returned uid 0. No SSH configuration changed.
The original symlink is recorded in that private directory's `previous-release`.

Independent baseline: [Actions 44412865906](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44412865906),
request commit `3aae66ce7916b431dfa48f5a5ef72b3211702c96`, hosted Ubuntu runner,
pinned host identity, all three units active, same original release.

## Cloud initiation from the connected GitHub tools

1. Read current GitHub `main` and live release; write/review the needed script
   under `ops/admin-scripts/<name>.py`. Commit and run checks first.
2. Add **only one new file** `ops/admin-requests/<32 lowercase hex>.json` in
   a separate owner commit to `main`. Fields:

   | Field | Required value |
   | --- | --- |
   | `schema` | integer 1 |
   | `request_id` | filename's exact 32 hex characters, never reused |
   | `source_sha` | request commit's exact parent SHA |
   | `expected_current` | freshly observed live release's 40 hex SHA |
   | `script` | previously committed `ops/admin-scripts/<name>.py` |
   | `script_sha256` | SHA-256 of that exact script's bytes |

3. `netcup-owner-admin.yml` runs on GitHub-hosted Ubuntu. Read the run, jobs and
   decoded logs through connected GitHub tools. The baseline status workflow
   remains available through its own `ops/requests/` channel.
4. Do not commit new requests/docs while an admin job is running: it rejects a
   changed main. Before each release switch it independently rechecks GitHub
   from the VPS and compares the live symlink atomically.

The request accepts **no command text**, arguments, external URLs or code from
messages. It selects already reviewed owner-committed Python. Root scripts can
install/update Debian packages, configure services, inspect processes/logs,
back up, deploy, roll back and recover within the owner's authorization. Never
include secret reads in public Actions output; keep data/backups in private
root-controlled VPS evidence directories.

## Enforced boundaries

- Exact repository, `main`, push actor and rerun actor `vl-ktk`, first attempt only.
- PR/fork events cannot trigger the secret-bearing workflow. A request commit
  may not modify code/workflows or include any additional file.
- Immutable parent source, fixed script directory, bounded request size, strict
  keys/types, duplicate-key rejection, exact script digest and payload digest.
- Source security scan, workflow policy and all Linux unittests before SSH.
- Read-only GitHub token; no persisted checkout credentials; pinned Actions.
- Existing SSH secret stays on hosted runner; strict pinned Netcup host key;
  fixed IP and deploy identity; no password or root SSH login.
- Remote root bootstrap locks concurrent jobs, refuses a changed release,
  verifies an in-memory archive snapshot, rejects links/traversal, extracts
  into a fresh private root directory, refuses request-ID reuse, and records
  start/result through `logger -t ae-owner-admin`.
- GitHub logs retain real stdout/results; private evidence is under
  `/root/ae-admin-jobs/<request_id>/evidence`. No data-bearing artifact upload.

This trusts owner-controlled main and the connected account with write access.
Full sudo means compromise of that account/key can compromise the VPS; it is
the owner's explicit chosen permission model. Existing manual SSH workflows
also use that identity and retain their owner/main gates. Old signed activation
code remains historical/prepared; it is not required by this channel.

## Live acceptance and recovery

`ops/admin-scripts/acceptance.py` records an online SQLite backup plus private
file snapshots, validates SSH restrictions and service health, verifies retained
backups, installs Debian `hello`, writes a root-owned test marker, and runs a
transient systemd service. It stages the checked wheel into a new immutable
release, deploys/restarts/health-checks, actually returns to the original release,
then activates the new release again. Data inventories (logical SQLite content
and file SHA-256) must match after each operation. A failed new-release health
check attempts one return to the original version. Live status is recorded
only after its run actually completes; source checks alone are not a PASS.

For future rollback use the reviewed `infra/deploy/promotion.py` compare-and-swap
interface inside a newly committed owner script, restart the controller and
check health; never copy an old database over working data during code rollback.
To revoke the newly added sudo rule, use the authorized provider root console,
move `/etc/sudoers.d/90-ae-deploy-admin` to the private bootstrap backup directory
and run `visudo -c`. This leaves existing SSH access intact.

The channel needs no Windows file/process/SSH session after the initial console
bootstrap. It executes when a cloud request arrives; it does not itself keep a
ChatGPT conversation/model running after the user's client closes. Completion
of this task does not authorize starting roadmap 6.14.
