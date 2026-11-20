# 7.11 — Shared controller capacity (source and isolated live acceptance)

**Status on 20.11.2026: `[ ] PARTIAL`.** Source GPT/Claude task adapters
have a pinned-specialist admission bridge; isolated **Netcup LIVE E2E PASS**
for one existing personal GPT and one Claude login is now verified, after
fixing disposable source ownership. The **persistent Netcup controller**
still does not dispatch production tasks through this gate. No new paid API,
production task-database mutation or unattended subscription use is authorized.

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

**One-shot isolated Netcup acceptance prepared 20.11.2026:**
`task_stage7_11_acceptance.py` creates an owner-approved fixed catalog,
team, specialist pins, task SQLite v17 and four disposable test tasks in a
temporary directory. With an explicitly selected shared `cpu_slots=1`,
`gpt=1 parallel / 2 reservations per 300s`, `claude=1 parallel /
1 reservation per 300s` policy, it checks CPU wait before any subprocess,
one GPT and one Claude adapter execution, GPT quota wait, and durable restart.
Default/CI mode uses deterministic local subprocesses and **zero model
requests**. `--live --owner-approved` requests **at most one real call per
provider**, without retry or printing model replies. These are deliberately
conservative **acceptance-only** numbers, not installed production policy.

The separate reviewed root owner job
`ops/admin-scripts/stage7_11_capacity_live_acceptance.py` checks exact
current core release, services, v17-compatible backup hook, and unchanged
working SQLite v14. It uses the existing `ae-runtime` ChatGPT OAuth grant and
official personal Claude CLI, allows a **single normal OAuth refresh** if
the previous GPT access token expired, and refuses paid API/alternative
provider routing. It copies reviewed source into a disposable private area;
the live core package, working task DB and systemd services stay untouched.
No client/provider output or tokens enter its GitHub Actions logs. A failed
preflight stops before model requests; a failed live run is never retried.

**Confirmed live evidence 20.11.2026:**
[owner #44807319708](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44807319708)
verified `OFFLINE_PASS` with readable runtime source; then
[owner #44808394097](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44808394097)
reported `LIVE_PASS` for one GPT and one Claude, shared CPU admission and
provider quota waits, durable disposable SQLite v17 and no automatic retries.
Existing subscription sign-ins were valid, paid API fallback was false.
Production core/services/task SQLite remained unchanged. The only test
policy numbers remain acceptance-only; they are not a live quota decision.

To complete 7.11 as **live** work, the trusted orchestrator must instantiate
the same policy for *every* provider dispatch, select machine-appropriate
numeric limits based on actually authorized subscription modes, recover
processes safely, and connect capacity waits to its real queue. On Netcup the
root-installed diagnostic backup hook was upgraded to v14–v17 compatibility
with an existing snapshot verify + isolated restore PASS (owner Actions run
`44796580391`). The live **core remains `0.3.11a0`**, its task SQLite remains
v14, and no live GPT/Claude orchestration or v17 migration has been performed.
Do **not** migrate the live DB solely to claim a milestone. The limited true
provider E2E is now verified, but remaining mandatory work is an owner-approved
numeric capacity policy, a reviewable persistent trusted dispatcher,
independent backup/restore and rollout/rollback checks before **any**
v14→v17 working SQLite migration or production promotion. This does not
close 7.7/7.7A/7.8/7.9 or initiate 7.12.
