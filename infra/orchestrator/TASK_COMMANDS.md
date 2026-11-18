# 6.7 — Task-bound shell/Git/compile/test commands

**Status:** offline-verified implementation, 18.11.2026. Source: `ai_agent_engine/task_commands.py`; checks: `tests/test_task_commands.py`. This does **not** enable arbitrary shell access for GPT/Claude and does not deploy a runner to Netcup.

## Trust boundary

Only a trusted coordinator may construct `TaskCommandRunner` with durable `TaskInbox`, `OwnerScope`, exact `task_id`/`worker_id`, the matching `TaskPermissionGate` and (optionally) `allow_trusted_tests=True`. The worker and workspace come from the existing SQLite `TaskAssignment`; the model cannot supply either. The task must be assigned to that worker and be `RUNNING`. Grants are checked and recorded by the existing gate **before** the subprocess runs. No command arguments, command text, interpreter scripts, paths or environment variables come from model JSON.

`allowed_tools()` exposes just `run_vetted_command({"command_id": "..."})`. Extra fields such as `argv`, `cwd`, `shell`, `env`, or unrecognized IDs are rejected by the existing tool registry/runner. There is no generic `shell`, `bash`, or `git` tool.

| Fixed command ID | Trusted argv purpose | Required actions | Limits |
| --- | --- | --- | --- |
| `git_status` | `git status --short --untracked-files=no` | `EXECUTE_TOOL` | 10 s |
| `git_diff_check` | `git diff --no-ext-diff --check --` | `EXECUTE_TOOL` | 10 s |
| `python_compile_check` | Compile syntax of workspace package/test `.py` in memory; no import | `EXECUTE_TOOL` | 15 s |
| `python_unit_tests` | `python -B -m unittest discover -s tests -q` | `EXECUTE_TOOL` **and** `WRITE_WORKSPACE`, plus trusted constructor opt-in | 60 s |

The first two Git operations add trusted `--no-pager -c core.fsmonitor=false -c core.hooksPath=/dev/null`; a fixed executable is resolved from `/usr/bin:/bin`, Git external diff is disabled, and Git global/system config, prompt, pager and optional locks are disabled. No `git add/commit/fetch/push/checkout/reset`, arbitrary build script, shell operator or network subcommand is permitted. `python_compile_check` is deliberately the minimal offline build/syntax gate rather than a general build backend. A future real package build requires a separately reviewed fixed command and its safety tests.

The subprocess receives a clean minimal environment (without inherited provider/API/OAuth credentials), no interactive stdin, a checked `0700` directory inode pinned through a POSIX dirfd as its cwd, a new process group and a bounded timeout and **4096-byte stdout**. Failure, timeout or output overflow stops the child and produces a sanitized exception; stderr is not forwarded into a task transcript. SQLite permission audits record the authorized action, not raw command output. Unknown commands are denied before audit or process start.

## Limits and subsequent sandbox in 6.8

- Python **unit tests execute Python code**, and `allow_trusted_tests=True` remains an owner-side trust decision. Since **6.8**, this runner **does** require Bubblewrap's filesystem/PID namespace and libseccomp socket/escape filter, with no unsandboxed fallback; the source integrity and full side-channel profile are not proven. See [6.8's measured boundaries](./TASK_EXECUTOR_ISOLATION.md).
- This sandbox has not been deployed on Netcup; system compatibility and other protections must be separately verified before any live untrusted executor use. An orphaned descendant may outlive an exited supervisor; **6.9** addresses process cleanup after crash. Permissions are still application grants, not a substitute for the OS boundary.
- Git worktrees and Python sources are read from the assigned workspace. The preexisting task assignment/workspace is trusted; this adapter does not automatically add command tools to the already working GPT/Claude executors.
- No real provider API was invoked. No Netcup, production database, access rights, secrets, paid APIs or deployed release were changed.

## Reproducible checks and rollback

`python3 -m unittest tests.test_task_commands -v` exercises real private temporary Git repositories, task permission/audit, Agent tool invocation, bad IDs and attempted shell injection, wrong worker/owner/state, missing/expired grants, test opt-in, malicious fsmonitor hook, malicious inherited environment, symlink root replacement, bounded output, timeout and actual unittest/compile runs. Full regression: `python3 -m unittest discover -s tests -q`.

Base before 6.7: `c4af65fd983d15353a48cec298e0292d57e8cb03` on GitHub `vl-ktk/ai-agent-engine/main`. Revert only the relevant 6.7 changes from the then-current `main` using a new reviewed revert commit (no force-push or destructive reset). No Netcup rollback is involved.
