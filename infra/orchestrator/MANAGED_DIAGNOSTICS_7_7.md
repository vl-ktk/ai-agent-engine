# 7.7 — single-owner diagnostic runtime candidate (NOT installed)

The previously approved private project and its first diagnostic backup are
real Netcup changes. **This runtime candidate has not been activated.** It is
not a replacement for the live `0.3.11a0` controller, not a live LLM task loop,
and not acceptance of the full 7.7 failure-to-incident/QA requirements.

## Exact boundary

- `ai_agent_engine/diagnostic_managed_runtime.py` owns one preapproved
  `SQLiteTaskStore` / `TaskInbox` (exclusive Linux `flock`), one
  `ProjectLeaderStore`, an empty-grants `TaskPermissionGate`, and an inert
  `SingleProjectDiagnosticRunner` in a **single OS process**. That process
  serves the existing `TaskDiagnosticAdapter` over the private `AF_UNIX`
  socket, not through another SQLite writer.
- Read-only host preflight must pass *before* writable SQLite is opened:
  exactly `vl-ktk/ai-agent-engine`, owner-approved registry and project DB,
  zero in-flight tasks, private mode/UID, source checkout pinned to
  `741fc1a4e62b52922611e1e28d4805d727dffe4d` and clean Git.
- `run_next()` is **never called** by the service; no automatic task
  admission, model call, command execution, incident fabrication, grant,
  provider credential, arbitrary repair or automatic retry. An invalid socket
  request must receive `HOLD` and must not create an incident.
- Private socket `/run/ai-agent-engine-diagnostics/intake.sock` is `0600`
  under a `0700` directory, same-UID `SO_PEERCRED`. Systemd prepares its
  runtime directory and performs shutdown; existing project workspace, task
  SQLite and backups are not moved or reset.
- The proposed additional unit is separate from `ae-controller.service`,
  keeps the original symlink and installed package unchanged, uses a pinned,
  root-owned read-only private code tree and Python `-I`, and limits network,
  namespaces, memory, CPU and process count. It starts **one** task-inbox
  owner, not a standalone diagnostic sidecar.

## Activation gate — HOLD

### Dedicated signed add-on path (prepared source; NOT installed)

The owner authorized a distinct release-signing identity and restricted
diagnostic activation on 20.11.2026. The dedicated **private** key was stored
in the GitHub repository Actions secret `AE_RELEASE_SIGNING_PRIVATE_KEY`;
its matching **public-only** allowed signer is pinned by the reviewed
`bootstrap_diagnostic_signing_7_7.py` source. The new fixed
`infra/deploy/diagnostic_addon_operator.py` refuses every source except
`60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23`, every live controller
except `0e04af007375d82daf8f7d3b36c71abb0c784e0b`, and any absent,
invalid, expired or mismatched owner signature. The signed intent uses a
separate `activate-managed-diagnostics-7-7` operation. Only its exactly
digested prepared wheel's Python source can reach the pinned root installer.

This path reuses the existing **manual** release-preparation and
wheel-transfer workflows; it has a separate owner-manual
`netcup-owner-diagnostics-activate.yml` with an offline `check-signature`
mode. It does not invoke the generic controller promotion operator or change
the existing controller symlink. The one-time public trust bootstrap is a
separately reviewed owner-admin request and **must not activate diagnostics**.
It does not modify sudoers: previously authorized Netcup admin permissions
already exist. A GitHub secret existing is not yet proof its OpenSSH encoding
and the Netcup trust path verify successfully; the offline signed check is
mandatory before installing any root files.

**Netcup update (20.11.2026):** the owner-manual signing self-test
[run 44735340945](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44735340945)
passed. The separately authorized, authenticated
[bootstrap run 44736497987](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44736497987)
reported `DIAGNOSTIC_SIGNING_BOOTSTRAPPED`: the root-only public allowed
signer, fixed add-on operator and pinned installer are present; the controller
release and old services remained unchanged, no new sudo/SSH rights granted.
**The diagnostic service itself is still NOT installed or running.** It remains
on HOLD until manual wheel preparation, verified transfer, separate signed
activation and live probes each pass.

The source-reviewed candidate `ops/admin-scripts/activate_managed_diagnostics_7_7.py`
describes an **additive** isolated service installation, not an existing
production package release. No request under `ops/admin-requests/` has been
submitted for this candidate, and the script must not be invoked by an
automatic/admin job as a substitute for the [manual signed release gates](../deploy/GITOPS_NETCUP_CONTROL.md).
The existing [7.7 runtime policy](./DIAGNOSTIC_RUNTIME_7_7.md) expressly
requires manual owner-gated release-preparation, transfer and activation for
new runtime code. Before host execution, decide whether this isolated add-on
can be independently authorized with an equally strong signed deployment
intent, or whether to integrate a narrow reviewed release candidate into the
existing manual workflow. Do **not** silently promote all of `0.3.19a0` over
the live `0.3.11a0` controller.

The proposed admin script checks the exact unchanged old release, old baseline
services, private backup, pinned source, empty owner-approved state; it writes
only a new root-owned code tree and unit, probes the socket as `ae-runtime`,
verifies restart and guards against tasks being created. On a failure it
disables the **new** unit and moves new code/unit into private recovery
evidence, retaining old release, protected data and snapshot. These controls
are **source-only** until a separately accepted deployment gate calls them.

## Verification and remaining acceptance

- Linux offline tests: single DB lock and lock release; exact Git SHA and
  project scope; private config rejection; busy writer rejection; grant-less
  inert process; separate hardened unit and reviewed installer fingerprint.
- In the current local environment the kernel rejects creating an AF_UNIX
  socket (`EPERM`), so **live socket E2E/restart and resource readings must run
  on a permitted, owner-authorized host**. No local test proves a Netcup unit
  started successfully.
- Before any permitted activation, re-check GitHub `main`, live Netcup
  `current` SHA, current backup effectiveness and immutable candidate digests;
  do not touch other projects, Windows, secrets, existing backup scripts or
  old controller. Capture exact host evidence and the previous version for
  rollback.
- After this single-process substep, 7.7 still requires a separately scoped
  **real** managed FAILED→incident→verified-QA path, crash/recovery review,
  scheduled backup observation and measured CPU/RAM/disk use. 7.8 remains
  on HOLD until 7.7 is accepted.
