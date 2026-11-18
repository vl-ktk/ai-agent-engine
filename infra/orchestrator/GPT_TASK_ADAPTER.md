# 6.2 — scoped GPT task adapter, live acceptance (2026-11-18)

**Status: PASS for roadmap 6.2.** This is a single owner-triggered SIWC
task-to-model integration in the **existing GitHub Codespace**, **not** a Netcup
production deployment or authorization for unattended background subscribers.

## Source and contract

- Source: `vl-ktk/ai-agent-engine`, `main`. Initial code publication:
  `9934cf2ac659d7333fa4bbbf2655eea8d2b21eb1`; previous `main` for
  targeted rollback: `7bd9e4fd4c58a85b4bea0eba425a1e27314dbc62`.
- `ai_agent_engine/task_gpt.py` implements trusted `GPTTaskExecutor`:
  task accepted in a private `SQLiteTaskStore` → exact owner/project scope →
  immutable worker/workspace claim → one durable delivery ticket → isolated
  child process → durable `EXTERNAL_REQUEST` authorization audit → private
  request → verified child completion → delivery ACK and terminal task state.
- `ai_agent_engine/gpt_task_worker.py` calls the already existing
  `inference.request_answer` with ChatGPT-plan SIWC, `store=false`,
  `stream=true`. It accepts bounded JSON through stdin, never a shell or
  task-generated command. No model tools, automated retry, billed API key
  fallback or token export. A second delivery cannot repeat the request.
- `ai_agent_engine/task_storage.py` **private schema v6** adds
  `task_provider_audit`: task ID, GPT model ID, timestamp and one allowlisted
  outcome (`completed`, `quota`, `auth`, `unsupported`, `unavailable`,
  `worker_failure`, `invalid_result`, `timed_out`, `cancelled`). v5→v6
  migration is transactional. Prompts, provider responses and auth tokens
  are **not** written to this journal. Real 429/401/403 failure paths have
  only synthetic classification coverage; no quota failure was induced live.
- The successful answer is returned to **trusted Python caller memory**, not
  retained as a durable artifact. Stage **6.12** owns controlled durable
  artifact exchange. Tasks' terminal state and the sanitized GPT outcome
  audit are durable. This is **not** a public HTTP dispatch endpoint.

## Actual live verification

18.11.2026, existing owner-authorized Codespace `laughing disco` in
`vl-ktk/ai-agent-engine`:

1. `python3 -m ai_agent_engine.auth status` reported an existing account and
   ChatGPT plan permission **GRANTED**, with access token expired. No
   credential or OAuth URL was copied, printed in this report or committed.
2. Fetched GitHub `main`, detached the clean test checkout at exact commit
   `9934cf2` (no change to the previous `fix/staged-live-acceptance` branch).
3. Ran once: `python3 -m ai_agent_engine.task_gpt_acceptance`.
4. Terminal result: `GPT_TASK_6_2_LIVE PASS: one scoped GPT response,
   permission, completion, audit and SQLite restart` (exit status inferred
   from returned shell prompt and PASS branch of the acceptance command).
   The `gpt-6.1-sol` model response contained the requested `AE-62-OK` marker.
   Exactly one ticket/request, successful task state and `completed` ledger,
   one allowed permission audit, one `completed` provider event, and
   `succeeded` state after closing/reopening SQLite were checked by code.
5. All test data/SQLite were disposable under a private temporary directory;
   no production data were used. Codespace was explicitly **stopped** after
   the run; GitHub UI displayed `Codespace "laughing disco" stopped.`

The live response confirms this integrated path works **once**, including
existing authorization on this host. It does **not** establish reliability
under continuous load, actual quota reset behavior, OAuth logout/revocation,
Claude unattended permissions, production account migration or live Netcup
operation. These are outside point 6.2 and remain separately tracked.

## Checks and boundaries

- `python3 -m unittest discover -s tests -q`: **502 OK**, **1 skipped**.
  Of these, **17 new GPT-adapter tests** use disposable actual local
  subprocesses and synthetic model behavior. Includes idempotency with 32
  competitors, grants/denials, SQLite audit failure, failure/quota category,
  timeout, invalid output, inheritance of paid keys, restart and v5→v6.
- `python3 -m compileall -q ai_agent_engine tests/test_task_gpt.py`: PASS.
- `python3 scripts/check_workflow_boundaries.py`: PASS.
- `python3 scripts/check_public_source.py`: PASS, including new tracked code.
- `git diff --cached --check`: PASS. Remote code commit tree matched local:
  `effd3b59f5594bdda3fb46a15fffe049b1910379`.

No changes to Netcup controller, live SQLite, systemd, SSH, permissions,
secrets, other projects or paid API subscriptions. No second server rented.
The existing Codespace consumed a short period of its normal GitHub compute
quota while live testing; **actual bill and remaining quota were not checked**.
No new GitHub Actions CI pass is claimed without observing its jobs.

Rollback for the **code** is a targeted reverse commit from fresh `main`
against the 6.2 code changes, using the previous tree/commit IDs above;
no force push or server rollback. Any later documentation commit must be
considered separately. Next authorized roadmap point: **6.3**, only after
an explicit owner command.
