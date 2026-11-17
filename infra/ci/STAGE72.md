# Stage 4.2 / 7.2 — immutable release preparation (NO live deployment)

**Owner:** Vlad, one human. **Source of truth:** `vl-ktk/ai-agent-engine` main.
**Status:** release-preparation source and offline tests prepared; **real controller
activation and live release are BLOCKED**, because there is no long-running
controller executable and the owner has not separately authorized production
release. Stage 7.2 must not be marked fully finished until those gates are met.

## Safe manual workflow (prepared, not dispatched)

`.github/workflows/release-preparation.yml` runs **only** from `main`,
through `workflow_dispatch` by GitHub actor `vl-ktk`, with exact
`PREPARE_RELEASE` confirmation. It accepts:

- `selected_ref`: an exact 40-character lowercase commit SHA, or a full
  `refs/tags/<tag>` name with a restricted safe character set. Only commits
  reachable from the current `main` are allowed.
- `expected_main_sha`: the owner's exact 40-character current main SHA.
- `confirmation`: `PREPARE_RELEASE` (this **never** deploys).

It compares the live GitHub API main SHA, the workflow's `GITHUB_SHA`, and
the checked-out `HEAD`. Any disagreement fails the run; rejected inputs,
divergent commits, branch aliases and unsuitable tags do not build anything.

With credentials explicitly **not persisted** into the Git checkout, it
creates a **detached** worktree at the resolved, pinned SHA, runs the existing
tracked-file secret signature guard on the selected source, builds exactly one
wheel, and installs that wheel without external package lookup in a disposable
virtual environment. The isolated **offline demo** is run from outside the
source tree. This is **not** an application service or model request.

`scripts/release_preflight.py` records a `release-manifest.json` with
source SHA, requested reference, verified main SHA, wheel name, size, SHA-256,
and explicit `controller_service_activated: false`. Existing manifests are
never overwritten. Immediately before storing the artifact, the workflow
rechecks remote main HEAD and rejects concurrent branch changes. The
prepared wheel and manifest are stored as a short-lived Actions artifact
(seven days), **not** on Netcup, a package index or any production server.

## Boundaries and blockers

1. **No VPS or SSH:** no deployment secret is referenced; no root, sudo,
   systemctl, SSH, SCP, Ansible apply, new network port or paid model call.
2. **No actual controller launch:** `ae-controller.service.template` remains
   uninstalled and contains a placeholder `@ENGINE_EXEC_START@`. The only
   executable `python -m ai_agent_engine` is a finite offline demo.
   Stage 6.3 must provide a real tested long-running runtime first.
3. **No live dispatch proof:** the manual owner-only workflow cannot be
   launched as a PR by GitHub CI. Code, syntax and local regression tests can
   pass, but a first owner-triggered preparation run and artifact verification
   still need to be performed.
4. **No implication of admin settings:** repository rulesets and Actions
   administration visibility limits remain as documented in stage 6.4.

## To complete 7.2 later

- Verify the real controller's executable, health signal and service unit
  (dependency from 6.3).
- Under a **separate** owner approval, define and verify an immutable release
  promotion from the prepared SHA-256 manifest, with least-privilege deploy
  identity, an explicit non-root allowed start action, previous-release
  pointer, and rollback. Never accept arbitrary remote shell input.
- Perform an owner-triggered dry run and then an authorized live release,
  recording workflow run IDs, actual server state and rollback evidence.

**Code rollback:** revert PR #51 or restore pre-7.2
`abe34aa65ecc5d25e4eb7dae445cacc0cdc5c814`. No live VPS changes exist
in this stage.
