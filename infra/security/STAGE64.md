# AI Agent Engine — Stage 4.2 / 6.4: source-level workflow authority audit

Reviewed on 2026-11-17 against GitHub `main` `591947cf2b056880019b2c0847f7bc2856373dba`. Only Vlad is the human owner of AI Agent Engine; `ae-deploy` and `ae-runtime` are technical Linux identities.

## Verified GitHub Actions workflow source (8 files)

- All eight `.github/workflows/*.yml` explicitly declare **`permissions: contents: read`** for `GITHUB_TOKEN`; no job-level write-token permission override was found.
- Five workflows refer to an encrypted SSH or old root/bootstrap secret: `netcup-debian13-user-baseline.yml`, `netcup-one-time-bootstrap.yml`, `netcup-readonly-access-audit.yml`, `netcup-readonly-ansible-ping.yml`, `netcup-readonly-system-audit.yml`. Each uses **`workflow_dispatch` only**, with `main` branch, `github.actor == 'vl-ktk'` and a separate typed confirmation check.
- `ci.yml` runs on push/PR and does **not** request any VPS or root secret. `vps-automation-checks.yml` is offline syntax/config validation without VPS credentials. `netcup-external-network-probe.yml` is a narrow no-secret read-only connection test to this project's own VPS.
- `netcup-debian13-user-baseline.yml` can still apply two **non-root owner-user directory** changes after a manual owner-confirmed run. It is not an unrestricted deploy, but it is not a purely read-only job.
- **Retired root bootstrap:** `netcup-one-time-bootstrap.yml` historically used `NETCUP_ROOT_BOOTSTRAP_PASSWORD` for one-time account creation. The live SSH policy now forbids root/password SSH; the historical job's `if` has an explicit unconditionally false guard. The old source remains for audit, not execution. The secret's existence or deletion was **not** inspected or changed.
- No discovered current workflow performs a production application release, modifies repository secrets/permissions or uses sudo/root on the VPS in an unattended event.

## Regression guard

- `scripts/check_workflow_boundaries.py` is invoked on every PR/push from the offline CI workflow. It fails on a changed/unreviewed workflow set, a new GITHUB_TOKEN permission override, privileged non-manual triggers, secret-bearing automatic triggers, missing owner/main/confirmation conditions, or re-enabling the retired root bootstrap.
- `tests/test_workflow_boundaries.py` covers valid current source and representative violations. This is a useful **source drift check**, **not** a cryptographic authorization mechanism. Someone with permission to change both a workflow and its checks can alter the gate.
- The actual live VPS SSH and Netcup firewall settings from 6.2 are unchanged by 6.4. No new secrets, rights, server, subscription or app deployment.

## Visibility boundaries

- GitHub connector returned an empty rulesets list, but classic branch protection returned **403: Resource not accessible by integration**; repository-level Actions settings and environment permissions are **not exposed** to this connector. Consequently the review **cannot assert** that GitHub branch protection, environment approvals, owner-only repository edit rights, or default Actions permissions are enforced at the account level.
- A later separate owner-approved repository-admin audit may verify those settings. Do **not** silently change secrets, roles, admin settings or permissions to fill this gap.

## Decision

Stage **6.4 source/workflow policy is verified and guarded**. Actual secret/permission modifications and production application deployments remain actions requiring separate, explicit owner approval. Keep the above GitHub-admin visibility limitation in handoff and report.

Rollback of **source only**: GitHub main immediately before this change `591947cf2b056880019b2c0847f7bc2856373dba`. No VPS rollback is needed.
