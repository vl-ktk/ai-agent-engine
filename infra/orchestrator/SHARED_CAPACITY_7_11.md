# 7.11 — Shared controller capacity (source-only contract)

**Status on 20.11.2026: `[ ] PARTIAL / SOURCE ONLY`.** Neither a live
controller nor GPT/Claude subscription runtime is wired to this gate. No
production execution, new paid API or unattended account use is authorized.

## Single pool, not one process per avatar

`main_shared_capacity.SharedCapacity` is a trusted dispatcher-side admission
gate over the existing `TaskInbox`. The **existing** SQLite task queue remains
the source of approved tasks. `waiting(scope)` enumerates assigned, pinned
and non-held tasks in `priority DESC, receipt ASC` order. Logical agents do
not own processes by virtue of their role. Only a separately approved task
may receive a reservation, and the caller must explicitly use `run(...)`
before invoking its existing model adapter.

- `CapacityPolicy.cpu_slots`: total simultaneous controller process permits
  across **all projects, owners and providers** in one private SQLite inbox.
  These permits **limit admission**, they are not CPU affinity, cgroups or
  guaranteed vCPU utilization; production capacity must be set by the trusted
  operator to match the actual machine.
- `ProviderBudget`: only explicitly configured `gpt`/`claude` providers,
  maximum concurrent calls and maximum new call reservations per rolling
  window. This is a **conservative operator-supplied cap**, not a real-time
  provider billing/usage reading. It never enables paid API usage or bypasses
  service conditions for unattended subscription CLIs.
- Return values: `admitted` or safe `cpu_busy`, `provider_busy`,
  `provider_quota` (with a retry-after hint), `provider_not_enabled`,
  `clock_rollback`, `task_active`. A denied task stays `assigned` in the
  durable queue. No automatic alternate provider is selected and no request
  is sent. Stage **7.12** is responsible for bounded wait/retry scheduling.

## Trust and durability boundaries

The gate requires an existing approved **7.3 specialist pin**, matching
worker/owner/project and `ASSIGNED` state. It rejects project emergency
holds and superseded 7.4 transfers. It **does not replace** task permissions:
actual `EXTERNAL_REQUEST` still needs the existing `TaskPermissionGate`
and a trusted provider adapter. It does not modify task claims, workspace
grants, provider selection, roles, plans or billing.

Private task SQLite schema **v17** adds a single shared
`task_capacity_reservations` ledger (immutable call records, with at most one
active reservation per task). Atomic `BEGIN IMMEDIATE` reserves under the
existing single-writer inbox lock. Provider quota counts reservations,
including cancelled/failed calls: it is never refunded. The same policy
enforces limits across projects/owners served by this one engine DB.

**Crash safety:** a slot remains reserved across restart and elapsed time.
There is no TTL expiry or guessed PID reuse. The assigned worker may release
an exact reservation only when the task is terminal **and** its process ledger
is absent or confirms `stopped`. Unknown RUNNING or active PID means HOLD.
The trusted `run` wrapper retains this slot after exceptions until completion
or explicit recovery. A backwards wall clock denies new admissions to avoid
resetting the rolling provider window. Existing v1–v16 task DBs migrate
transactionally on first opening with new code. Migration is not reversible
without an independently validated database restore.

## Verified local scope and outstanding integration

The new offline tests exercise common GPT+Claude CPU slots, independent
provider concurrency and quotas, wait with no callback, cross-owner/worker
denials, two concurrent contenders, durable restart, clock behavior, exact
settlement and v16→v17 migration. No real GPT/Claude calls are needed.

To complete 7.11 as **live** work, the trusted orchestrator must call this
gate on *every* provider dispatch, select a machine-appropriate approved
policy, ensure verified process recovery and integrate capacity waits with
the existing queue. **Do not deploy task SQLite v17 to Netcup:** the last
confirmed live controller core is `0.3.11a0`, and the known pinned backup
hook has not been reviewed for schema v16/v17. Inspect and update the signed
backup/restore gate, live source SHA and rollback path first, then obtain the
required release approval and run an isolated live E2E. This source work
does not close 7.7/7.7A/7.8/7.9 or initiate 7.12.
