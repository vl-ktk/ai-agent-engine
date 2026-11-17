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
