# 7.7 — isolated diagnostics backup through the existing daily timer

This is the next gate after owner-approved first-project registration. It only
covers `/var/lib/ai-agent-engine-diagnostics`, not a new project or a diagnostic
runtime. **The existing `ae-backup.py`, `ae-backup.service`, `ae-backup.timer`,
controller and the original snapshots must not be replaced or reset.**

## Source and format

- `ai_agent_engine/diagnostic_backup.py`: standalone Python-stdlib-only backup
  of the exact approved project, three independently online-copied SQLite DBs
  (task inbox v14, registry v1, project leader v2), private runtime config,
  documents and pinned Git workspace. No secrets outside this private root.
- Destination: dedicated private
  `/var/backups/ai-agent-engine-diagnostics` (0700 `ae-runtime`), one snapshot
  per successful invocation; manifest, SHA-256, `PRAGMA quick_check`, archive
  member restrictions. Retention: at most 7 **verified** diagnostic snapshots;
  an invalid older snapshot prevents deletion. This is local-only, not an
  off-VPS disaster-recovery copy.
- `restore-smoke` extracts **only** into an automatically removed isolated
  `/tmp` directory, validates all three copied DB schemas and data integrity,
  checks preserved config digest, and never touches the real project files.
- Source snapshot is refused for a foreign project, dirty/incomplete schema,
  active/running diagnostic task, unsafe symlink/FIFO or insufficient space.
  Completed tasks are eligible for backup.

## Owner-gated host integration

After reviewed source, offline tests and Ubuntu CI, obtain a fresh Netcup
baseline and an explicit scope-limited owner permission for this production
backup change. Submit one authenticated `ops/admin-requests/<id>.json` via the
existing `netcup-owner-admin.yml` channel, pinning the reviewed
`ops/admin-scripts/enable_diagnostic_backup_7_7.py` and active Netcup release.

The one-time installer refuses drift in the three currently installed backup
files (SHA-256 prechecks), changed active release, occupied target paths, or
baseline unit failure. It then creates **only** the new executable and backup
destination and a reversible `ae-backup.service.d/70-diagnostic-project.conf`
drop-in that adds an `ExecStartPost` call and the two dedicated sandbox paths.
The preexisting `ExecStart`, timer and controller are untouched. Run a new
diagnostic-only snapshot, independent verify, disposable restore-smoke,
`systemd-analyze verify`, and baseline service checks. Do not activate or
restart the diagnostic service.

On failure, move only newly installed script/drop-in to the private admin job
evidence for recovery, run `systemctl daemon-reload`, leave existing backup
files and any already produced snapshot intact. **Never automatically delete
user snapshots during rollback.** The precise Netcup release SHA remains the
pre-operation rollback checkpoint; new drop-in can be removed or disabled only
by a subsequent owner-authorized maintenance action.

## Acceptance limits

Local test snapshots do not prove live timer execution. The first real
diagnostic snapshot and restore are separate from the next scheduled timer;
verify the next scheduled combined backup before claiming repeated automation
PASS. Off-host copy and full VPS loss recovery are not covered. The rest of
7.7 (socket, singleton process, failure-to-incident QA and resource overhead)
remains open; do not start 7.8.
