# Netcup GitOps control: read-only status and owner-approved releases

**Status: READ-ONLY STATUS LIVE PASS; SIGNED RELEASE ACTIVATION PREPARED ONLY.** The
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

## 2. Root-installed release operator and signed release intent

The source `infra/deploy/owner_release_operator.py` provides restricted
activation of one immutable release. It is **not installed on Netcup**.

Security hardening after reviewing the earlier source-only draft: knowing
a wheel's SHA-256 and having `ae-deploy` SSH access **must not confer root
permission to execute that wheel**. Otherwise a compromised deploy account
could supply its own wheel and checksum, then get root to run it during
`stage_release`. The operator now requires a **separately signed, short-lived
intent** matching the exact operation, source SHA, current SHA, and wheel digest.
The root-owned signers file is at
`/etc/ai-agent-engine/release-signers`. Its principal is
`ai-agent-engine-owner` and signature namespace
`ai-agent-engine-release@vl-ktk`. The signature uses OpenSSH
`ssh-keygen -Y sign/-Y verify`; it is independent of the SSH deploy key.

The root operator:
- rejects unsigned, forged, mismatched and expired intents; accepts
  no shell commands, arbitrary paths or alternate services;
- reads incoming files using bounded `O_NOFOLLOW` regular-file checks,
  rejects symlinks/hardlinks and snapshots wheel bytes into a **root-controlled
  temporary directory** before any pip install or import, closing the
  validate-then-swap race against writable incoming directories;
- compares wheel bytes to the **signed independent SHA-256** before the
  trusted root-owned `promotion.py` stages them;
- compares actual `/opt/ai-agent-engine/current` to the expected SHA,
  atomically switches to the staged release, restarts **only**
  `ae-controller.service` and checks its health under `ae-runtime`;
- on failed health attempts exactly one return to the previous release;
  a double failure blocks and requires manual recovery;
- requires a separate valid owner signature for an explicit rollback,
  but its own automatic health-failure rollback is part of the original
  approved activation.

**One-time admin/bootstrap remains mandatory** and needs specific
owner authorization because it changes privileged rights. Independently
review the exact source version, then install the operator and matching
`promotion.py` into `/usr/local/libexec/ae-release/` as immutable
root-owned files. Install the owner's dedicated **public** Ed25519 signing
key into the root-owned allowed-signers file in the format:

```text
ai-agent-engine-owner ssh-ed25519 <owner-release-signing-public-key>
```

Install a minimal sudoers rule limited to this exact root-installed
program (no generic root shell), validate via `visudo -cf`, and audit the
permissions on every ancestor and installed Python file:

```text
/usr/bin/python3 -I /usr/local/libexec/ae-release/owner_release_operator.py *
```

This wildcard permits arguments to the **fixed program only**. The fixed
program validates all options, exact SHA, current release and the signed
intent before any mutation.

Provision a **separate dedicated signing private key** into the
GitHub Actions secret `AE_RELEASE_SIGNING_PRIVATE_KEY`, using owner-only
GitHub settings. Do not reuse `AE_DEPLOY_PRIVATE_KEY`. Never paste signing
keys, OAuth tokens or private SSH keys into ChatGPT/GitHub source/logs.
Until both the root public key and the Actions private key exist,
activation must fail closed.

This proposal does **not** install sudoers, signers, change GitHub secrets,
or perform privileged commands on Netcup. It is not proof of a working release
path.

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
4. After independent owner validation, the workflow signs a bounded intent
   using the dedicated release key (15-minute expiration), transfers only
   that signed intent over the existing pinned-host SSH channel, rechecks
   `main` and invokes the root-installed restricted operator.
5. The operator checks the public signing key, signature, exact request
   identity, real old SHA and signed digest, then stages/promotes and checks
   health. On failure it attempts rollback.
6. For an explicit rollback, choose `rollback` and an existing staged SHA.
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
- Live release: one-time root installation, public signing key and distinct
  GitHub Actions signing secret **after separate owner approval**; then
  owner-dispatched signed immutable release promotion, health and rollback. Existing
  runtime/project SQLite must remain unchanged.
- No secrets stored in repository or chat, no new paid API or VPS, no Netcup
  changes made while preparing these sources.

## Security boundary and verification status

- Read-only status transport: **LIVE PASS**, GitHub Actions
  [run 44402946575](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44402946575)
  confirmed actual Netcup SHA and all three active units on 18.11.2026.
- Privileged signed activation: **SOURCE ONLY**; requires secret/bootstrap
  setup and separate live acceptance. No root rights granted by this commit.
- Subscription-based GPT/Claude E2E 6.14: still **LIVE BLOCKED**.
