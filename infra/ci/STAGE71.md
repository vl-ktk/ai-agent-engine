# Stage 4.2 / 7.1 — offline CI: build, tests, secret scan, Ansible

**Scope:** one owner (VL), source GitHub `vl-ktk/ai-agent-engine` main.
All CI steps use an unprivileged GitHub-hosted runner, read-only repository token,
and no VPS SSH keys, paid model calls or production deployment.

## CI gates

1. `.github/workflows/ci.yml` runs on PRs and main: Ubuntu / Python 3.11,
   3.12, 3.13, and Windows / Python 3.12. It compiles application and tests,
   runs `scripts/check_public_source.py` against tracked Git files,
   checks reviewed workflow permissions with `check_workflow_boundaries.py`,
   executes all offline unit tests **including synthetic integration and staged
   acceptance tests** (fake model transports and mocked Claude CLI), and runs
   the core demo from source and from an installed package.
2. A distinct Ubuntu build job produces a real Python wheel using
   `pip wheel --no-deps`; `pyproject.toml` pins the isolated setuptools build
   backend. The job installs the built artifact using `--no-index --no-deps`,
   checks metadata and runs the installed demo **outside the checkout**.
   No artifact is deployed to the VPS or uploaded to package indexes.
3. `.github/workflows/vps-automation-checks.yml` validates the offline
   Ansible example inventory and each of four existing playbooks via
   `--syntax-check`, plus shell syntax and inert systemd template markers.
   The only actual `--check --diff` run targets `verify.yml` with
   `inventory.offline-ci.yml`, whose target is explicit localhost, with local
   Ansible connection and no credentials; it tests Ansible check-mode execution,
   **not Debian 13 behavior or actual VPS reachability**.

## Important limits

- Secret scan checks tracked paths and known high-confidence token signatures,
  not every conceivable secret; never interpret PASS as proof of zero secrets.
- The Debian 13 baseline is syntactically checked in CI but cannot be
  meaningfully applied to Ubuntu CI as a Debian 13 non-root operator. Its actual
  `--check --diff` execution and repeat idempotency were previously verified
  on the real VPS in stage 5.3 by an **owner-gated** workflow.
- Read-only audits use server-specific packages/facts and are **not executed**
  by PR CI. Nothing in 7.1 grants CI permission to access the VPS or run CD.
- GitHub account Actions settings and branch protection were not accessible via
  the installed GitHub integration in 6.4; workflow source guards alone do
  not imply those admin settings are enforced.

## Approval / recovery

Stage 7.1 modifies repository CI files only. No live VPS, secrets,
operating-system permissions, firewall or working data are changed.
Recover with a GitHub revert of the Stage 7.1 merge commit or restore
the pre-merge source SHA `5a548a8b25aaf0ad0672fb1ce0ead9459f3d4629`.
Stage 7.2 CD and 7.3 separation remain open and require separate handling.
