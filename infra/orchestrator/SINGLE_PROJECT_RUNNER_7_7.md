# 7.7 — One-process single-project diagnostic task runner (source-only)

**Status: offline verified only; Netcup/Windows unchanged.** This is a narrow
first task executor for `vl-ktk / ai-agent-engine`, not an always-on general
GPT/Claude agent, installation script, systemd service, network API or
production acceptance. It cannot approve an owner, create server credentials,
start paid providers or turn task prompts into executable commands.

## Concrete execution boundary

- A trusted owner-side runtime opens the **one** exclusive `TaskInbox` and
  existing approved `ProjectLeaderStore` and passes both to
  `SingleProjectDiagnosticRunner`, together with its preprovisioned `0600`
  diagnostic config and a `TaskPermissionGate` belonging to the same inbox.
  Project identity must be exactly `vl-ktk / ai-agent-engine`. Existing
  `bind_inprocess_diagnostics` checks task SQLite inode, source/workspace
  binding, approved registry, parent permissions and one project. This code
  never provisions missing live state or opens a second writer.
- `run_next()` selects at most **one** already admitted, dependency-ready,
  highest-priority queued task. If any old `ASSIGNED/RUNNING` work needs
  review, it refuses new execution; no automatic orphan/retry replay. Only
  one `SingleProjectDiagnosticRunner` can bind a given open TaskInbox and
  concurrent calls are refused rather than running two workers.
- The worker is the fixed ID `diagnostic-7-7`. At the moment the trusted
  runner claims its pinned Git workspace it rechecks exact commit and dirty
  tree. The task title and prompt are **not interpreted**.
- Its default enabled task action is a *read-only*, fixed `git diff --check`.
  A separately owner-confirmed, startup-only QA candidate also permits one
  second **hard-coded** negative check, `git cat-file -e` for
  `.ae/qa-7-7/owner-approved-required-marker`, after proving that file is
  not tracked by the approved clean HEAD. Its real nonzero exit is recorded
  explicitly as an **expected QA test incident**, never a production defect.
  Neither mode accepts arbitrary paths or commands from a prompt.
  No shell, dynamic argv, Git network operation, repository source execution,
  remote model, write tool, arbitrary test, patch or deployment. The Git
  binary is from trusted `/usr/bin:/bin` and runs with a scrubbed environment,
  disabled hooks/fsmonitor/external diff and no inherited provider secrets.
  This limited check does not need to run arbitrary repository code in a
  Bubblewrap sandbox; future wider tools **must** use the existing 6.8
  OS sandbox and explicit grants, not this narrow read-only allowlist.
- Before the Git child is allowed to exec, `TaskProcessController.launch_gated`
  durably registers its exact process identity, sets `RUNNING` and invokes
  the explicit `TaskPermissionGate` for `EXECUTE_TOOL`. Grant denial closes
  the dormant gate and cancels the process; it does **not** invent a defect.
  The check is bounded to 15 seconds and no raw output is captured.
- Actual exit 0 → `SUCCEEDED` and durable delivery ACK. Actual nonzero exit
  with unchanged Git source and verified stopped process group → single
  failure ACK with `max_attempts=1`, durable `FAILED`, then one bounded,
  sanitized diagnostic occurrence via `TaskDiagnosticAdapter`.
  Timeout, changed checkout, unexpected signal, missing grant, uncertain
  recovery or storage failure → HOLD/owner review, no automatic re-execution.
- Task ledger and project diagnostics are separate SQLite transactions. A
  failure of the latter does **not** roll back already recorded `FAILED`;
  a trusted owner may reconcile evidence with the original baseline/signal,
  but never re-run an unknown completed effect.

## Explicitly not activated

There is no entry-point on Netcup and no automatic event loop for GPT/Claude.
The diagnostic runner uses pre-approved disposable/offline task records only.
Its process-exit tests verify this fixed Git check, not general-purpose AI
execution or post-reboot recovery on a live VPS. Before a server activation:
confirm a real authenticated owner channel and project registry; approve the
exact runner unit and private SQLite; inspect the installed SHA and rollback;
perform host resource measurements and security QA; then use the **manual**
owner GitHub release workflows. No changes to Windows or Netcup from this
source-only substep. Stage 7.7 remains `[ ] PARTIAL / LIVE HOLD`, 7.8 not begun.

An additional [read-only host preflight](./HOST_PREFLIGHT_7_7.md) is available
from source `0.3.19a0` to report missing provisioning and unsafe/inconsistent
local state; it does not perform that provisioning or activate the runner.

## Subsequent source-only owner QA candidate (20 November 2026)

`diagnostic_managed_runtime.py` now has an **optional Python-only**
`OwnerConfirmedDiagnosticQA` argument. It is not accepted by the existing
CLI, local socket or original deployed service. A future independently signed
owner operator must authenticate the exact receipt/proposal digest *before*
passing that argument. The currently installed signed v1 operator and
installer are pinned to the older runtime and **cannot** deploy this revision.
Within the single owner process it admits one task only if the inbox is empty,
constructs one 90-second `EXECUTE_TOOL` grant and invokes `run_next()` once;
normal startup still has zero grants. Source pin, runtime DB lock, no replay,
the intentional negative case and actual diagnostic occurrence have local
Linux fixture tests. Abrupt-kill recovery, signed v2 rollout and Netcup E2E
are **not verified**, so 7.7 stays PARTIAL and server task data remains untouched.
