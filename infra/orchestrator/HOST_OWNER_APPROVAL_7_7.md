# 7.7 — exact first-project approval (source-only, pending owner action)

The private one-project host tree is already provisioned on Netcup, but its
owner-approval registry remains empty. **Do not repeat initial provisioning.**

## The immutable proposal

- Owner: `vl-ktk`
- Project: `ai-agent-engine`
- Title: `AI Agent Engine`
- Proposal ID: `proposal_8950a54bb02d4537943c5ed6f3286696`
- Proposal SHA-256: `8443ee2fd8de2cb0c893a39420b0229b463a055b014f3bf93c571952f573dd56`
- Reviewed payload: [owner proposal](../../ops/owner-proposals/ai-agent-engine-7-7.json),
  file SHA-256 `2423890a811b7ac9f2b5b7f09479772cf38a1091bb1170fc5ee4142abf0d9650`.

This file is **not an approval**. Merely publishing the proposal or running
offline tests does not create a real owner decision.

## Owner-controlled registration gate

Only after the owner explicitly approves the **exact** proposal ID/hash and
separately authorizes the scoped Netcup registry write:

1. Recheck current GitHub `main`, reviewed registration source, Linux CI,
   immutable active Netcup release SHA and rollback.
2. Create an owner-authored, single-file `ops/admin-requests/<id>.json`
   command (parent SHA, expected live release, exact path and SHA-256 of
   `approve_single_project_7_7.py`, currently
   `59d4c99aebbd7b0c611a33ffbe27e126216f4ccf4c65645645df45b4b8bfd742`).
   Do not commit an administrative request
   as part of source preparation; that would execute against Netcup.
3. Existing `netcup-owner-admin.yml` verifies `github.actor`,
   `github.triggering_actor`, owner push, one-file request, pinned script,
   fresh main and the pinned SSH host before executing on Netcup.
4. The script rechecks baseline services, owner/project/config, empty task
   database, unmodified prepared Git source, private paths and exact proposal.
   It drops effective UID to `ae-runtime` to apply `ProjectLeaderStore.approve`,
   writes its result to private admin evidence and reports `APPROVED_INERT`.
5. Independently check registry/leader SQLite identity, schema v2 and source,
   confirm the existing controller/backup/host-check services were unaffected.

An owner-gated GitHub action is the authenticating transport here. The script's
`OwnerDecision` object is only an internal typed representation *after* that
verified action; an AI-generated object or arbitrary chat statement alone
cannot authorize a host write.

No systemd diagnostic process, new grant, task, Unix socket, paid API,
retention or external archive is activated by this registration step.
Backup coverage for the separate diagnostic tree, socket parent, one-process
runtime and end-to-end QA/overhead measurements are independent remaining
7.7 gates. Windows and other projects stay out of scope.

**Status:** source preparation only; authenticated owner command and Netcup
registration not yet executed. 7.7 remains PARTIAL / LIVE HOLD.
