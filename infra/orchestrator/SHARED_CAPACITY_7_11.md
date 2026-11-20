# 7.11 — Shared controller capacity (source-only contract)

**Status on 20.11.2026: `[ ] PARTIAL`.** Source GPT/Claude task adapters now
have a pinned-specialist admission bridge (offline E2E verified), but the
live Netcup controller and provider sessions are **not** wired to it. No
production model execution, new paid API or unattended account use is authorized.

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

## Provider adapter integration (source-only)

`GPTTaskExecutor` and `ClaudeTaskExecutor` accept an explicit trusted
`SharedCapacity` instance constructed over **the same** private `TaskInbox`.
For each 7.3 specialist-pinned dispatch they check the approved provider,
deny a missing capacity gate, and reserve the shared CPU/provider budget
**before** obtaining a delivery token or launching the subprocess.
`cpu_busy`, `provider_busy`, `provider_quota` and other admission refusals
return `GPTTaskOutcome(executed=False, capacity_reason=...)` while the task
remains `ASSIGNED`, without consuming an attempt or starting a child.
Terminal execution settles the exact slot; crashes and unknown running
processes retain it for recovery. Duplicates never spend a second quota.

The historical unpinned 6.2/6.3 personal pilots remain executable *only*
when no capacity manager was supplied; supplying one forbids unpinned tasks.
This backward-compatibility path is **not** a general 7.11 dispatcher.
Production must construct **one** shared policy/DB for every trusted adapter
and must not invoke standalone personal pilots as a parallel admission path.
This module does not install that dispatcher or authorize unattended CLI use.

## Verified local scope and outstanding integration

The new offline tests exercise common GPT+Claude CPU slots, independent
provider concurrency and quotas, wait with no callback, cross-owner/worker
denials, two concurrent contenders, durable restart, clock behavior, exact
settlement and v16→v17 migration. No real GPT/Claude calls are needed.

To complete 7.11 as **live** work, the trusted orchestrator must instantiate
the same policy for *every* provider dispatch, select machine-appropriate
numeric limits based on actually authorized subscription modes, recover
processes safely, and connect capacity waits to its real queue. On Netcup the
root-installed diagnostic backup hook was upgraded to v14–v17 compatibility
with an existing snapshot verify + isolated restore PASS (owner Actions run
`44796580391`). The live **core remains `0.3.11a0`**, its task SQLite remains
v14, and no live GPT/Claude orchestration or v17 migration has been performed.
Do **not** migrate the live DB solely to claim a milestone; first review
release/source/rollback and obtain limited true provider E2E with approved
accounts. This source work does not close 7.7/7.7A/7.8/7.9 or initiate 7.12.
