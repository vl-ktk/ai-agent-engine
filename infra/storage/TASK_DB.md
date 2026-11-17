# Stage 4.16 — SQLite task store and reversible migrations

**Scope:** versioned local task metadata on the existing Netcup Debian 13 VPS. The controller remains independent; this stage does not start a queue, assign agents, expose an API, register any real project, or store secrets.

## Database path and ownership

- Default path: `/var/lib/ai-agent-engine/projects/engine.sqlite3`.
- Owner/process: `ae-runtime`. New database file: `0600`; parent `/var/lib/ai-agent-engine/projects`: `ae-runtime:ae-runtime`, `0700` (live verified in 4.15).
- No write through `ae-deploy` or a model tool. No real project rows are created during initialization.
- The SQLite database is shared internally by the engine. Every task read/write primitive takes an explicit `project_id`; foreign keys enforce same-project parents/dependencies. **Not yet an authorization boundary**: a process with direct DB access or a caller authorized for one project but incorrectly passed another project ID could read other rows. The actual grants and access checks are separate later tasks.

## Schema and version contract

Uses SQLite `PRAGMA user_version` plus a migration-history table, and `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`. All schema changes use explicit `BEGIN IMMEDIATE` / `COMMIT` or `ROLLBACK` (not `executescript`).

| Version | Changes | Reverse operation |
| --- | --- | --- |
| 0 → 1 | `schema_migrations`, `projects`, `tasks`, `task_dependencies`, indexes; validated project-scoped foreign keys, P0–P3 priority, optimistic revision | Destructive reversal to version 0 is unsupported |
| 1 → 2 | `task_events` and scoped index | 2 → 1 only if `task_events` has **zero** rows; otherwise `UnsafeRollback` with no schema change |

`migrate()` is idempotent, refuses versions newer than the program and refuses unversioned files containing unrelated tables or inconsistent migration history. The first database initialization is non-destructive: an already existing file is never truncated.

The current list of statuses is an initial storage constraint from the previously discussed architecture. It does **not** implement or imply approval of workflow state transitions; those are stage 5. No automatic deletion/cascade of task rows with children or event rows is allowed. `delete_task()` is a scoped storage primitive for later authorized use; no real live tasks were deleted.

## Safe commands

From the correctly installed package (or an **integrity-verified exact GitHub source** in a temporary directory readable by `ae-runtime`):

```bash
runuser -u ae-runtime -- python3 -m ai_agent_engine.task_db smoke
runuser -u ae-runtime -- python3 -m ai_agent_engine.task_db init
runuser -u ae-runtime -- python3 -m ai_agent_engine.task_db check
```

The `smoke` action operates on an automatically removed isolated temporary database and tests schema creation, CRUD, rollback and integrity; it never uses the production path. `init` creates/updates **only the named database** and is safe to repeat. `check` refuses missing or incompatible schema and checks integrity, without running migrations. Both must run under `ae-runtime` on the VPS, **not root**, so ownership remains correct.

For tests on a dev machine:
```bash
python -m unittest tests.test_task_db -v
python -m unittest discover -s tests -q
python -m ai_agent_engine.task_db smoke
```

## Recovery and limitations

- Before any later migration that may change populated data, first prepare an independently verified backup (stage 4.18) and record the exact schema version.
- On a disposable test database, `migrate(db, 1)` explicitly rolls version 2 back to 1 if no `task_events` exist; `migrate(db, 2)` re-applies. It never reverses populated event history.
- A real-world database backup/restore/rotation is not yet implemented and remains assigned to 4.18–4.19.
- Code is committed to the GitHub source; **do not say the running controller was updated** unless a new version-locked release is independently deployed and verified. Initialization of the standalone DB does not change `ae-controller.service`.

## Live Netcup verification — 2026-11-18

- VPS `v2202611460959561466` (Debian 13), previously running `ae-controller.service` version `0.3.8a0`; no controller deploy/restart or existing DB alteration.
- Pinned source: `71907236cf0aa15bfedac6b776a213c84a915383`, `ai_agent_engine/task_db.py`; downloaded as `/tmp/ae-task-db-71907236.py` and SHA-256 independently verified against the source: `6d60ec9d8513d75a7e5f5323415f5b1a8b1d01b47bcf37fa9d8a958630f366`.
- `runuser -u ae-runtime -- python3 /tmp/ae-task-db-71907236.py smoke` → `task DB disposable CRUD/rollback smoke: PASS`. This creates, tests and reverses only a disposable temporary DB; **no rollback executed against live data**.
- Same runner `init` → `task DB schema=2, integrity=ok`; runner `check` → `task DB schema=2, integrity=ok`.
- `stat -c '%U:%G %a %n' /var/lib/ai-agent-engine/projects/engine.sqlite3` → `ae-runtime:ae-runtime 600 /var/lib/ai-agent-engine/projects/engine.sqlite3`; parent previously checked `0700` in 4.15.
- `systemctl is-active ae-controller.service` → `active`; `systemctl --failed --no-pager` → `0 loaded units listed`.
- Source version `0.3.9a0`, GitHub source commit `71907236...` and [CI #44189539204](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44189539204) SUCCESS. **Runtime controller remains `0.3.8a0`; the new task DB module is not installed into its running release.** Future wiring belongs to later steps.
