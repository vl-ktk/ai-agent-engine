# Debian 13 baseline for AI Agent Engine controller VPS

Scope: only the owner-operated Netcup Debian 13 controller VM. This is not the GPU/125B host, KTK Social, ATHL or iPad.

## Verified compatibility and expected packages

- Debian 13 (trixie) defaults to Python 3.13, per Debian release notes and the official `python3` package.
- The repo's AI Agent Engine core requires Python 3.11+; the user-space baseline refuses an unexpected OS, account or older Python.
- `ansible-core 2.19` supports Debian 13's Python 3.13 target. The existing offline CI currently tests syntax using `ansible-core==2.21.5`; keep future controller pin compatible with the team's CI.
- Required system tools before production deployment: `python3` (already usable via the successful Ansible ping), `python3-venv`, `ca-certificates`, `git`. Optional operations tools when needed: `sqlite3`, `rsync`, `jq`, `curl`.
- **The above is an approved *package inventory*, not proof packages are installed.** Installing apt packages or adding sudo privileges is deferred to a separately authorized administrative step.

Sources:
- https://www.debian.org/releases/trixie/release-notes.en.pdf
- https://packages.debian.org/trixie/python3
- https://docs.ansible.com/projects/ansible/latest/reference_appendices/release_and_maintenance.html

## User-space baseline, requiring no administrator access

`infra/ansible/debian13-user-baseline.yml` gathers OS facts and asserts Debian 13, `ae-deploy` and Python 3.11+. It then creates only:

- `/home/ae-deploy/ai-agent-engine-runtime/` (private mode 0700)
- `/home/ae-deploy/ai-agent-engine-runtime/releases/` (private mode 0700)

It does **not** modify root authentication, SSH, system packages, sudoers, firewall, systemd, running applications, private OAuth data, backups or paid services.

An owner-gated manual GitHub Actions job can run `--check --diff`, apply this exact playbook, then rerun `--check --diff` to prove idempotency (`changed=0`). It uses only encrypted `AE_DEPLOY_PRIVATE_KEY`, a previously pinned host key and read-only repo permissions. No local iPhone console instructions.

Remaining before production: decide limited package-install mechanism, install missing packages only with separately granted permission, harden the host, configure backups, process isolation and release deployment. Record each real run ID and do not claim completion from offline syntax checks.
