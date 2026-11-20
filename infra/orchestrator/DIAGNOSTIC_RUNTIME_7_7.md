# Stage 7.7 — private diagnostic intake (preparation, NOT live activation)

**Owner scope:** the requested 7.7 integration on the existing Netcup VPS, no
Windows changes, paid provider calls, secret/permission modifications or broad
production-data migrations. This source is **not** an active task runner.

**Owner decision 20.11.2026:** the initial allowed scope is **only** the
AI Agent Engine project. Its internal identifiers are
`owner_id=vl-ktk`, `project_id=ai-agent-engine`, accepted by `OwnerScope`. The runtime now
requires **exactly one** project and fails closed on multi-project config.
This is a selection of scope, **not** an authenticated `OwnerDecision`, grant,
or proof that the workspace exists on Netcup.

**Source-only update `0.3.16a0` (socket fix `0.3.16a3`):** the standalone diagnostic CLI returns
`standalone_task_owner_forbidden` and **does not** open a second task SQLite.
The trusted task-process owner must call
`bind_inprocess_diagnostics(config, inbox=inbox, scope=scope, leaders=leaders)`
on the owner thread to get an adapter and socket path for synchronous
`record_failed_task` calls, then run `serve_local_intake(adapter, path, stopped)`
on a thread **inside the same process**. The optional
`run_configured_service(config, stopped, inbox=inbox, scope=scope, leaders=leaders)`
requires these same trusted references. Both
methods use the existing TaskInbox and ProjectLeaderStore objects; neither
opens another TaskInbox/registry. The binding is refused if owner/project,
task database inode, workspace root, registry root, source directory or
private file permissions disagree with the pre-provisioned configuration.
The CLI/service template must not be installed; its restart loop is disabled.
This bridge is still **not an automatic source of FAILED events**.

**Source-only `0.3.17a0`:** a trusted task-owner process may opt into
`TaskDiagnosticAdapter.finish_managed_process(controller, task_id,
success=..., baseline_commit=..., signal=...)` after a worker child has exited.
The supplied `TaskProcessController` must own the very same open `TaskInbox`.
The controller verifies the actual isolated process-group exit and its return
code before durably transitioning the task to `SUCCEEDED`/`FAILED` as its
assigned worker. An actual `FAILED` then passes through the already-scoped
adapter with Git provenance and idempotent diagnostics. Successful completions
produce no diagnostic. Incorrect return status, foreign ownership, invalid
Git baseline, or a task/evidence mismatch are held. The trusted runtime must
retain the pre-execution baseline; a model-supplied SHA is not provenance.

**Two SQLite transactions are not atomic:** if recording the diagnostic fails
after the task is `FAILED`, never retry/relauch the completed process. A trusted
owner may reconcile the *evidence* with `record_failed_task` using the same
pre-execution baseline and signal. A crash that loses those inputs is HOLD for
owner review, not an automatic replay. This callable is not a task execution
loop or a Netcup deployment. The server still has no live TaskInbox owner.

**Source-only `0.3.18a0`:** the opt-in
[`SingleProjectDiagnosticRunner`](./SINGLE_PROJECT_RUNNER_7_7.md) provides a
bounded, real Linux offline task/exit producer for *pre-admitted* tasks. It
uses the existing exclusive inbox, approved registry, scope, permission gate,
delivery ledger and process controller. Only one fixed read-only Git check
is enabled; it never reads task prompts as instructions. This is **not** a
production Netcup task-loop or a credential-bearing GPT/Claude executor.

The non-secret source-only [configuration example](./diagnostic_runtime_7_7.single-project.example.json)
illustrates the intended **future** private state directory. The example
cannot be fed directly to a running service: the host operator must first
approve/provision the exact project registry, workspace, Git binding and
private task DB, then install the config as `0600` under the correct host UID.
Do not create or share a second project as part of this first rollout.

## Implemented source-only component

- `ai_agent_engine/diagnostic_intake_service.py`: bounded newline-delimited JSON
  failure intake, `AF_UNIX` only, `SO_PEERCRED` same-effective-UID check,
  `0600` socket under an already-private `0700` directory, one request per
  connection, maximum 2048 bytes, serial processing, timeout, clean shutdown.
- Explicit owner/project scope is loaded only from a **pre-provisioned** private
  `0600` configuration, not from a request or model. Config is strict-schema;
  there are no provider tokens, grants, shell commands or network listeners.
- The runtime requires an **already prepared** private task SQLite at the
  current exact schema and an **already owner-approved** `ProjectLeaderStore`.
  It refuses an absent DB, schema migration, absent project approval or
  unexpected source binding. It does not approve projects, admit tasks or
  start workers. No arbitrary path from an event is read as evidence.
- `TaskDiagnosticAdapter` requires the same durable task inbox, accepted
  `FAILED` transition from the assigned worker and actual Git source of the
  trusted assigned workspace. `ProjectDiagnosticsStore` independently enforces
  project source binding, bounded safe evidence, idempotency and three-attempt
  HOLD. On uncertain restart, `recover_unknown` holds incomplete attempts.
- The systemd source template is **not installed**. Its state and socket paths
  are isolated from the existing controller. No Netcup settings were changed.
  The obsolete separate-process template is now explicitly non-deployable.

## Strict runtime boundary

`SQLiteTaskStore` has an exclusive `flock`; two independent daemons cannot
share it as live writers. The local intake must be owned by the **one trusted
task process** controlling `TaskInbox`. Simply enabling a new sidecar service
is **not permitted**: it would compete for the lock or fail to observe tasks.
The source-only systemd template is obsolete and will not serve as a release
unit. A future host runtime must run task execution and bounded diagnostic
intake in one process, with an authenticated owner/project gate.

There is currently **no live task execution loop** on Netcup supplying such
events. An event-socket receiver alone does not complete 7.7 live acceptance.
The existing `ae-controller.service` is heartbeat/systemd lifecycle only.
No fake diagnostic incidents or simulated model responses may be presented as
real GPT/Claude QA.

## Required owner-controlled activation sequence (not executed)

1. Bind exact owner/project identity through a trusted authenticated owner
   approval channel; pre-provision the private project registry and verified
   local repository/workspace. Do not fabricate `OwnerDecision` from model text.
2. Define the sole runtime owner of the dedicated task SQLite, the exact
   compatible existing event producer, Unix-socket path, storage quotas and
   stop/rollback behavior. Do not reuse live task databases across processes.
3. Recheck installed Netcup release SHA and provision a backup/rollback point.
   Last observed read-only at 20.11.2026: `0e04af007375d82daf8f7d3b36c71abb0c784e0b`
   (`0.3.11a0`), not a standing guarantee.
4. Build a candidate from the precise deployed source with approved 7.5/7.7
   dependency closure. GitHub `main` `0.3.18a0` contains additional offline
   stages and must **not** simply be promoted blindly as a live orchestrator.
5. Have the GitHub owner manually run pinned `release-preparation.yml`, then
   `netcup-owner-release-transfer.yml` and
   `netcup-owner-release-activate.yml` using verified source/wheel/main/live
   SHA inputs and signed release intent. Never bypass manual gates with the
   owner-admin workflow or an arbitrary root script.
6. After separately reviewed host configuration and the **single task-owner**
   runtime installation (not the obsolete diagnostic sidecar),
   perform a bounded synthetic, then authenticated real failed-task E2E with
   exact Git provenance, the one approved project scope, restart/recovery, audit,
   and measured CPU/RAM/disk before declaring 7.7 complete. Roll back any
   failed release to the freshly reverified previous server SHA.

## Verification limits

Offline parsing, private-config rejection, source/worker validation and
runtime construction are exercised in Linux tests. AF_UNIX socket binding is
blocked (`EPERM`) in the current sandbox, so the socket E2E test is
**explicitly skipped** here; it must pass on a permitted isolated Linux host
before any Netcup activation. Neither the current service nor systemd template
was installed/enabled on Netcup. No server disk percentage can be stated
without actual measurements. Status **7.7 PARTIAL / RUNTIME NOT DEPLOYED**.
