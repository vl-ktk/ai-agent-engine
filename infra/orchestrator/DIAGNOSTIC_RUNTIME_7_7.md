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

## Strict runtime boundary

`SQLiteTaskStore` has an exclusive `flock`; two independent daemons cannot
share it as live writers. The local intake must therefore be owned by the
**one trusted task process** controlling this `TaskInbox`, or operate on a
separately prepared dedicated DB with a trusted producer explicitly feeding
events into that same process. Simply enabling a new sidecar service alongside
an active task runner is **not permitted**: it could lock the queue or fail to
observe newly admitted tasks. The source-only systemd template is for future
owner review, **not** approval to enable an idle service.

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
   dependency closure. GitHub `main` `0.3.14a0` contains additional offline
   stages and must **not** simply be promoted blindly as a live orchestrator.
5. Have the GitHub owner manually run pinned `release-preparation.yml`, then
   `netcup-owner-release-transfer.yml` and
   `netcup-owner-release-activate.yml` using verified source/wheel/main/live
   SHA inputs and signed release intent. Never bypass manual gates with the
   owner-admin workflow or an arbitrary root script.
6. After separately reviewed host configuration and unit installation,
   perform a bounded synthetic, then authenticated real failed-task E2E with
   exact Git provenance, separate project scope, restart/recovery, audit,
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
