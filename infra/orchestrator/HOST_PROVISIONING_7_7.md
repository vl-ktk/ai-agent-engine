# Stage 7.7 — inert first-project host provisioning

**Owner-approved task scope:** `vl-ktk / ai-agent-engine` only. The source-only
admin script [`provision_single_project_7_7.py`](../../ops/admin-scripts/provision_single_project_7_7.py)
is a **one-shot, opt-in** target for the existing locked `netcup-owner-admin`
GitHub workflow. It is not a background service and may run only when the
current immutable server release exactly matches a fresh owner-observed SHA.

## Intended state after successful execution

- `/var/lib/ai-agent-engine-diagnostics` (private `0700`, owner `ae-runtime`)
  with `runtime.json` (`0600`) selecting exactly one project, no secrets.
- A new, empty `tasks.sqlite3` at known `TaskInbox` schema v14 and a separate,
  empty `projects/project-leaders.sqlite3` registry v1. Both are private
  `0600`. **No project is approved or admitted by this initialization.**
- A Git checkout in `workspaces/ai-agent-engine` pinned to the reviewed source
  commit (not a generated baseline). No other project is cloned, fetched or
  installed; every source file and parent directory remains private.
- No task, grant, incident, worker, archive, retention job, socket, systemd
  unit, AI provider or service restart is created. Netcup's active release
  and existing backup/controller/timer services remain unchanged.

The script refuses a pre-existing target, wrong active release, missing core
services and unexpected symlinks/hardlinks. A failure after creating the new
directory moves the **entire newly-created tree** into the private root job
evidence directory for manual recovery; it does not erase it or touch other
projects. The administrator channel itself stages a signed-in, immutable
source archive and stores private evidence; those transport writes are
separate from the application's private working state.

## Still required before running 7.7 live

1. An **authenticated exact** `OwnerDecision` for the concrete project
   proposal ID/title/hash, recorded by a trusted owner interface. Chat text
   or a generated `OwnerDecision` is not proof of such approval. Until then
   the project registry intentionally remains empty and host preflight HOLD.
2. Provision the project-specific leader store via the trusted 7.5 approval
   path, including private `documents` and `leader.sqlite3` schema v2.
3. Establish backup coverage for the new directory, verified source pin,
   explicit runtime grants and the single authorized TaskInbox process.
4. Install an approved, bounded one-process service using the in-process
   diagnostic adapter; never start the legacy diagnostic sidecar against the
   same task SQLite. Verify safe failure/incident/QA/rollback E2E before any
   production activation or cost-bearing AI execution.
5. Repeat live SHA check immediately before activation, use the reviewed
   owner-only release workflow and measure actual incremental CPU/RAM/disk
   overhead. Baseline host-wide measurements alone cannot predict this.

**Status:** `[ ] 7.7 PARTIAL / LIVE HOLD`. Windows is outside this step.
