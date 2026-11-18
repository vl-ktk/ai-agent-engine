# Netcup GitOps control: read-only status and owner-approved releases

**Status: READ-ONLY STATUS LIVE PASS; PRIVILEGED ACTIVATION NOT DEPLOYED.** The
scoped GitOps request launched GitHub Actions run 44402946575 on 18 November
2026. SSH returned three active service/timer states and the live release
85375103800845eeff696e53f510c66c428408ed. Only the read-only channel
has been verified; do not mark privileged activation, rollback or 6.14 PASS.

## Existing starting point

The current production controller last verified on 17–18 November 2026 was
`0.3.8a0`, release `85375103800845eeff696e53f510c66c428408ed`.
The existing `ae-deploy` SSH identity is stored in GitHub Actions secret
`AE_DEPLOY_PRIVATE_KEY` and has no sudo rights. There is a verified
host-key fingerprint `SHA256:irOFPt5ymeILMXOe0cRyHYwY39XkmwxZ0OJkPh20QJg`.
Production may since have changed; read it again before release operations.

## 1. Scoped GitHub-push status command

`.github/workflows/netcup-gitops-status.yml` is the one reviewed exception
to the original manual-only SSH workflow policy. A new commit to `main` that
**adds exactly one** `ops/requests/<32 lowercase hex>.json` file causes the
workflow to validate the exact one-file diff and JSON:

```json
{"schema":1,"operation":"status","request_id":"0123456789abcdef0123456789abcdef","server_ipv4":"REPLACE_WITH_NETCUP_PUBLIC_IPV4"}
```

The placeholder is **not a valid request**. Use the server's verified public
IPv4; the request ID must be new and match the filename. No other operation
names are recognized. Changes to scripts/workflows in the same commit are
rejected, as are merges, extra files, noncanonical/private IPs, duplicate JSON
keys, and symlinks. The workflow accepts only `main` pushes attributed to
`vl-ktk`. It has read-only GitHub permissions, uses the existing SSH key only
after matching the pinned Netcup host fingerprint and executes one fixed
non-root remote command:

```sh
systemctl is-active ae-controller.service ae-backup.timer ae-host-check.timer; readlink /opt/ai-agent-engine/current
```

No sudo, shell input, package installation, service restart, or persistent
daemon is created by status. Whether GitHub App commits are attributed to
`vl-ktk` and whether this push-triggered job executes successfully must be
observed LIVE. A CI PASS alone cannot prove that. The status file contains
only the host's public IPv4, never SSH credentials.

**Security tradeoff:** this is a deliberately narrow automatic secret-bearing
workflow. Unlike historical manual-only jobs, every eligible main push with
one valid request can invoke the non-root read-only SSH command. Only the
fixed target host identity may receive the key authentication.

## 2. Restricted root-installed release operator (not installed)

The operator source is `infra/deploy/owner_release_operator.py`. It:
- accepts only `activate` or `rollback`, exact 40-hex source/old versions,
  an independently supplied SHA-256 and a fixed confirmation token;
- checks the actual `current` release against the expected old SHA;
- for activation, uses the **already existing** trusted `promotion.py`
  to verify the owner-reviewed wheel and stage an immutable release;
- atomically switches only `/opt/ai-agent-engine/current`;
- restarts **only** `ae-controller.service`, checks systemd and owner-scoped
  `ae-runtime` controller health;
- on health failure attempts one return to the previous release, then reports
  an error. No automatic retries or arbitrary shell execution.

**One-time admin/bootstrap required:** an authorised Netcup root operator must
independently verify and install both source files under
`/usr/local/libexec/ae-release/`, owned by root, not writable by ae-deploy,
plus an exact sudoers rule allowing **only**:

```text
/usr/bin/python3 -I /usr/local/libexec/ae-release/owner_release_operator.py *
```

for `ae-deploy` as root, with `NOPASSWD`. The trailing wildcard still
allows *arguments* but not changing the executable; Python rejects unknown
flags, commands, paths, and values. Use `visudo -cf` and validate real ACLs.
This is a change to privileged access and must be separately confirmed
before installation, not performed by this source patch. Root-owned operator
updates need the same review. Do not copy a GitHub-controlled script to a
root-executable path through an untrusted shortcut.

The helper cannot stop an arbitrary service, modify firewall, read private
secrets through an API, or select different deployment directories.

## 3. Owner-gated activation and rollback

`.github/workflows/netcup-owner-release-activate.yml` is **manual
workflow_dispatch only**, owner `vl-ktk`, with exact confirmation
`ACTIVATE_VERIFIED_RELEASE`. It requires exact current `main`, selected
committed source SHA, the real previously observed VPS release SHA, independent
wheel digest and public IPv4. The selected SHA must be an ancestor of `main`.

Sequence for a new release:
1. Use existing `release-preparation.yml` with owner confirmation.
2. Use existing `netcup-owner-release-transfer.yml` with owner confirmation;
   this only copies the verified wheel/manifest into `ae-deploy` incoming.
3. After the privileged helper is installed and live old SHA recorded, run
   `netcup-owner-release-activate.yml` with owner approval.
4. Recheck `main`, pinned SSH identity, actual VPS old SHA, stage/promote and
   health. If health fails, the helper attempts rollback.
5. For an explicit rollback, choose `rollback` and an existing staged SHA.
   It cannot roll back an uninstalled artifact.

**Not yet autonomous deployment:** preparing and transferring artifacts still
require owner dispatch. A push-triggered status check does not grant
deployment rights. Replacing manual release approval with safe unattended
authorization is a separate security decision, not part of this source patch.

## Validation and blockers

- CI: `python -m unittest tests.test_netcup_status_request tests.test_owner_release_operator tests.test_workflow_boundaries -v`,
  `python scripts/check_workflow_boundaries.py`, full Linux suite.
- Live: one new scoped status request on `main`; GitHub Actions status job
  must complete with a real SSH result. Confirm actual workflow actor.
- Live release: one-time root installation and review; owner-dispatched
  immutable release promotion, controller health and rollback. Existing
  runtime/project SQLite must remain unchanged.
- No secrets stored in repository or chat, no new paid API or VPS, no Netcup
  changes made while preparing these sources.
