# HANDOFF — 20.11.2026, Secure Vault 7.7A / source-only реализация 0.3.21a0

**Поручение:** после согласования owner-chat сценария начать реализацию всего
7.7A непрерывно, не навязывать отдельную форму/повторное подтверждение,
не менять секреты, права других проектов, Netcup/Windows до безопасной
приёмки. Источник: GitHub `vl-ktk/ai-agent-engine`, исходный `main`
`0b7bca075ef28fdd3577be31419ab06ccb5b985d` для точечного отката.

**Готовый результат в исходниках:** новый `ai_agent_engine/secure_vault.py`,
`tests/test_secure_vault.py`, версия пакета `0.3.21a0` с зависимостью PyCA
`cryptography`, [описание](./infra/security/VAULT_IMPLEMENTATION_7_7A.md)
и актуализация 7.7A в `IMPLEMENTATION_ROADMAP.md`. AES-256-GCM в private
SQLite, произвольные категории, per-project области, scoped grants,
общие secret IDs, автоматическое сохранение новых ключей исполнителями
при действующем grant, Get/Update/Revoke/List, архив/перемещение,
защищённые зашифрованные in-memory backup и тестовый offline restore.
`VaultOrchestratorBridge` принимает байты от **уже аутентифицированного**
owner adapter, без отдельного подтверждения; он не подключён к ChatGPT.

**Открыто `[ ] PARTIAL / SOURCE ONLY`:** нет реальной ChatGPT/MCP инжекции,
host key provider с unattended unseal и аварийным owner key, внешней
зашифрованной копии с проверенным off-host restore, production ACL/определения
подлинной роли, master-key rotation, real QA, подписанного Netcup release.
Ни одна фаза 7.7A и общая приёмка ещё не `[x]`. Никаких настоящих паролей,
ключей, secret migration, новых прав, рабочих данных, Netcup/Windows,
других проектов и расходов не касались. Предыдущий известный Netcup core:
`0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`
(не свежий замер, перед любым live release проверить заново).

**Offline проверки:** отдельные security/backup/ACL тесты и общий `unittest`,
workflow scanner, public-source scan, wheel build, diff check — точные
результаты публикуются в итоговом отчёте; это не live E2E. При приёмке
сначала подключить реальный owner-authenticated ingress, утвердить отдельно
режим ключей/новые права, off-host backup и проверить подписанный rollout.

---

# HANDOFF — 20.11.2026, Secure Vault 7.7A / устранение лишних ограничений, DOCS ONLY

**Текущее поручение владельца:** убрать четыре мешающих требования Secure Vault:
обязательную отдельную форму передачи пароля, повторные подтверждения
повседневного использования, обязательный credential broker/запрет на
сознательную передачу секрета оркестратору в чате и ручной ввод ключа Vault
при каждом штатном рестарте. Основной сценарий для будущей реализации:
владелец пишет пароль главному оркестратору → тот сохраняет и использует
его автоматически в ранее разрешённой области, не повторяя значение и не
навязывая ротацию только из-за чата. Сохранены шифрование, scoped-права,
аудит без значений, off-host backup, аварийный owner-held recovery key,
ограничения на новые права и необратимое удаление.

**Изменения:** только требования в `infra/security/SECURE_VAULT_PLAN.md`,
краткая запись `7.7A` в `IMPLEMENTATION_ROADMAP.md` и эта передача.
Исходный GitHub `main` перед изменением:
`25f3a346be1324fbea61ebce4e032c7034172ffc` (для точечного отката
именно этой документационной правки после повторной проверки HEAD).
**Не реализовано:** Secure Vault, API, хранение/передача секретов и
автоматическая разблокировка сервиса остаются требованиями будущей работы.
Никаких изменений Netcup, Windows, ключей, секретов, прав, рабочих данных,
других проектов или расходов. Исторический live Netcup controller:
`0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`
(не новый замер). Статус 7.7/7.8 не менялся.

---

# HANDOFF — 20.11.2026, AI Agent Engine / Secure Vault P1 roadmap insertion, DOCS ONLY

**Owner request:** plan, do not implement, the new Secure Vault module.
Added independent **7.7A** between existing 7.7 and 7.8, without changing
their wording, renumbering the historical 169 direct subtasks, or introducing
third-level task numbering. The full requirements, five unchecked
phases and ten acceptance checks are in
`infra/security/SECURE_VAULT_PLAN.md`; the compact parent task is in
`IMPLEMENTATION_ROADMAP.md`. No Vault runtime, secret migration, permissions,
password changes, Netcup/Windows deployment, paid API or other project changed.
Source baseline before these **documentation-only** edits:
`7e2e92aab280bb1b08bd411516c76216c1a20515` (source rollback reference).

**Independent historical status after the older notes below:** Netcup signed
one-shot real FAILED→QA incident [run 44749623203](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44749623203)
PASS; read-only live resource snapshot [run 44751047718](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44751047718)
PASS (0.037% one CPU core in 3 s, cgroup RAM 17,043,456 bytes, one process).
Core `0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`
unchanged. First *naturally scheduled* diagnostic backup on 21.11.2026
~03:00–03:06 UTC remains `DEFERRED`, **7.7 not marked complete**. Owner
explicitly allowed moving to 7.8 meanwhile; source-only 7.8 work had **not**
started when the owner switched to this Vault documentation request.

---

# HANDOFF — 20.11.2026, AI Agent Engine / owner-requested future release updater improvement (DOCS ONLY)

**Current scoped task:** record, without implementing, a permanent signed release
update mechanism in `IMPLEMENTATION_ROADMAP.md`, after acceptance of 7.7 and
before the next diagnostic-code upgrade. The new unchecked unnumbered backlog
item specifies owner approval, immutable signed manifest, arbitrary successive
reviewed versions without root-operator rewrites, exact release pin, safe
rollback and negative signature/TOCTOU tests. The 169 numbered subtasks and
current 7.7 acceptance requirements are unchanged. No host, workflows, keys,
sudo grants, databases or runtime code are changed by this documentation task.

**Fresh previously verified evidence (20.11):** Netcup owner-signed v2 gate
[44746313425](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44746313425)
SUCCESS after reviewed source merge `f4baca74c7e2f487a892f184a3048096d1179e12`;
admin request commit `9e92750d84d68ce757e3197260cfdb6464c7c610`.
Prepared `0.3.20a0` wheel [44745909554](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44745909554)
and staged transfer [44746144569](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44746144569)
SUCCESS. Old signed operator preserved privately, controller rollback reference
`0e04af007375d82daf8f7d3b36c71abb0c784e0b` / `0.3.11a0` unchanged.
**The signed one-shot live QA has NOT been dispatched**; resource measurements
and first scheduled diagnostic backup remain open: 7.7 PARTIAL / 7.8 HOLD.
Previous source-only notes below are historical, not the latest live status.

---

# HANDOFF — 20.11.2026, AI Agent Engine 7.7 / signed v2 gate and one-shot QA candidate, NOT DEPLOYED

## Authorized exact trust-gate update — still source-only

The owner explicitly authorized changing **only** the installed protected
diagnostic signing allowlist to accept source
`3ef07c51ec287a24bd54d8fcea7c40a99e69b569`, retaining old trusted
operator bytes for rollback. This does not authorize other projects,
production controller promotion, new sudo/SSH rights, new secrets or paid API.

Separate new reviewed files (old v1 gate files remain unchanged):

- `infra/deploy/diagnostic_addon_operator_v2.py`: same Ed25519 trust, fixed
  source `3ef07c51`, pinned `0e04af00` controller, new one-operation intent
  `upgrade-managed-diagnostics-7-7-v2`, exact candidate wheel/runtime digest,
  new root installer digest.
- `ops/admin-scripts/authorize_diagnostic_v2_gate.py`: **authenticated**
  owner-admin bootstrap, validates exact root old operator SHA-256, signer and
  baseline, preserves old operator in private evidence, atomically updates only
  the existing sudo-target file, adds new immutable installer file. Does not
  deploy code, restart a service, or change an SQLite database.
- `ops/admin-scripts/upgrade_managed_diagnostics_7_7_v2.py`: root-pinned
  signed installer stages **new isolated** source tree; stops only
  `ae-managed-diagnostics.service`, runs one owner-confirmed expected-negative
  real Git subprocess inside a hardened transient singleton systemd unit,
  confirms one durable FAILED task, one allowed scoped grant, exhausted
  delivery and one QA-labelled diagnostic occurrence, then switches/restarts
  only the isolated diagnostic unit. Keeps the v1 code and old unit in private
  evidence. Unexpected task/process state is HOLD; never restores/deletes
  live SQLite or restarts the old core. Potential task crash requires owner
  recovery; this **is not live-proven** yet.
- `.github/workflows/netcup-owner-diagnostics-upgrade-v2.yml`: manual owner
  dispatch, pre/post `main` SHA checks, host SSH pin, independent Ed25519
  signing key, fixed source and remote pinned sudo path. Old signed v1
  workflow unchanged. Workflow security allowlist extended narrowly.

**Do not dispatch any root bootstrap or signed upgrade until** source passes
the full Linux CI/wheel and a fresh live preflight confirms old core/unit,
runtime code and private task state. Separate commits are required:
source-only reviewed code → immutable owner-admin request for gate bootstrap →
reviewed preparation/transfer of exact `3ef07c51` artifact → fresh manual
signed upgrade. If bootstrap/upgrade cannot be proven safe, HOLD and record
the reason; no generic admin-runner bypass. Existing rollback core
`0e04af007375d82daf8f7d3b36c71abb0c784e0b`; old signed operator SHA-256
`7d2779ccdf55c464ea31e6e52f340dae0cbdaee23d009298e08cf7a760582207`.
No server change has occurred for v2 yet; 7.7 PARTIAL / 7.8 HOLD.

---

# HANDOFF — 20.11.2026, AI Agent Engine 7.7 / owner-confirmed QA candidate 0.3.20a0, NOT DEPLOYED

## Current code candidate — controlled managed QA without a second task owner

Owner explicitly authorized **one** bounded test task and a scoped
`EXECUTE_TOOL` grant for the already registered `vl-ktk/ai-agent-engine` and
asked to complete the safe remaining work together. The isolated worktree is
based on GitHub `main` `220d8b84a25e231de464d2e947142319df26205b`;
the original user's dirty checkout was not changed.

The source-only candidate adds a trusted `OwnerConfirmedDiagnosticQA` Python
argument to `open_singleton()` and `run_managed()` (not a CLI/socket feature):

- Default deployed path remains **inert**, zero grants/tasks/models/network.
- Exact trusted owner receipt and fixed proposal digest are required, with
  separate approval content for success and intentionally negative QA.
- One idempotent fixed task is admitted **only if the owner-scoped inbox is
  empty**; one expiring `EXECUTE_TOOL` grant targets only
  `diagnostic-7-7`; the very same exclusive task owner invokes `run_next()`.
- Normal clean Git `diff --check` produces genuine `SUCCEEDED`, no incident.
  The optional **explicitly expected negative QA**, bound to a distinct
  approval digest, invokes real read-only `git cat-file -e` on one hard-coded
  absent marker. On nonzero exit it records genuine `FAILED` and a clearly
  QA-labelled incident with evidence, without dirtying the project Git source.
  If that marker is actually present, the task is refused before admission;
  changes in source identity fail closed. No artificial socket failure.
- No task is retried automatically. Unknown/crashed active work still needs
  trusted owner review before service restart; **live rollout is not cleared**.

**Tests verified in isolated local fixtures:**
`python3 -m unittest tests.test_diagnostic_managed_runtime_7_7
tests.test_single_project_diagnostic_runner -v`: real Git subprocess
SUCCESS/FAILED→incident, one exclusive SQLite owner, exact approval, no
replay/dirty source checks PASS. Whole local suite: **900 tests OK, 2 skipped**;
source and workflow security scans and compileall PASS; isolated wheel
`ai_agent_engine-0.3.20a0` built successfully (local SHA-256
`803df55b46224f2ccb081991f344e2117a8b577bd86b862b6eb51f1b14ed9fe4`).
These tests did not touch Netcup. The candidate code has **not been installed**
and must not be used as a live release until a distinct source/checkpoint
and signed release gate are approved and evidenced; the currently installed
root operator is pinned to older source `60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23`
and cannot accept this candidate. Do **not** route this runtime change through
the generic owner-admin workflow as a shortcut. New separate signer/installer
review, crash recovery plan and host resource checks are still required.

**Latest verified live rollback reference (unchanged by this candidate):**
core `0.3.11a0`, SHA `0e04af007375d82daf8f7d3b36c71abb0c784e0b`;
add-on source `60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23`, active
`ae-managed-diagnostics.service` per signed owner run `44737886631`.
Scheduled backup first natural cycle still `DEFERRED` to 21.11.2026 03:00
UTC. **7.7 remains PARTIAL; do not advance to 7.8 or touch Windows/other
projects.**

---

# HANDOFF — 20.11.2026, AI Agent Engine 7.7 / isolated diagnostics ACTIVE, 7.7 PARTIAL

## Current blocker — real managed FAILED→incident QA needs an authorized task producer

**Read-only code assessment at source `60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23`, verified 20.11.2026:**
`ai_agent_engine/diagnostic_managed_runtime.py:run_managed()` calls
`open_singleton()` then `serve_local_intake()`; it **never** invokes
`SingleProjectDiagnosticRunner.run_next()`, creates no task admission,
does not issue `EXECUTE_TOOL` grants and has no automatic worker loop.
`diagnostic_intake_service.process_local_event()` requires a previously
durable *real* `FAILED` task and authenticated same-process owner; a forged
socket failure must be rejected and must not count as a live incident.
The standalone component's real subprocess FAIL tests were offline, not on
Netcup. Latest installed release and first socket/restart live probes are
documented below; no new Netcup test was performed by this read-only review.

**Safe next implementation before meaningful live QA:** review and test a
one-time trusted task admission + explicit owner-scoped grant/execution path
**inside the same already-owned task process**, with provenance of a real
verification failure and a verified incident/QA readback. Protect the
previously registered single project, existing project Git source, runtime
SQLite/registry and backups; never open a second task writer or inject a fake
incident. Any new runtime code must pass offline tests and the separate
owner-signed release process. **Permission boundary:** creating a task/grant
writes into existing live SQLite; changing those working records needs the
owner's specific consent to the exact controlled test. If no legitimate real
failure occurs, report `NO_FAILURE_OBSERVED`, not a synthetic PASS.
**Current status: 7.7 PARTIAL / LIVE QA BLOCKED**, no 7.8 or Windows/other
project changes. Scheduled diagnostic backup observation remains `DEFERRED`
only until the normal 21.11.2026 cycle as recorded below.

---

## Owner-approved 7.7 sequence exception — 20.11.2026

Owner explicitly answered **yes** to deferring **only** the first naturally
scheduled diagnostic backup observation until the next `ae-backup.timer`
cycle, while preparing the independent **real managed FAILED → incident →
verified QA** path inside 7.7. The reason is timing: diagnostic backup hook
installed after 03:00 UTC today, next 03:00–03:06 UTC on 21.11.
**Risk:** scheduled `ExecStartPost` could still fail; first manual snapshot,
SHA verification and three-DB restore-smoke PASSED but do not replace this
evidence. No destructive live data change until a scoped owner-approved test
and safe recovery are established. **Return condition:** after next cycle,
read-only inspect timer/service history and independently verify a *new*
private diagnostic snapshot; keep `DEFERRED` until then, not `[x]`.
`IMPLEMENTATION_ROADMAP.md` updated in docs-only SHA
`e7d92e8462b0a1f26c9aa20e0cb6dc3b42b889ba`.

**Exact installed runtime boundary:** pinned source
`60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23` opens one `TaskInbox`
and serves a private socket but deliberately never calls `run_next()`,
never admits tasks or creates `EXECUTE_TOOL` grants.
The live socket `HOLD` probe and a restart are **not** real FAIL→incident QA.
The next independent 7.7 step is to review a safe, bounded, authenticated
real-worker QA path without a second SQLite writer or fake failures.
Baseline core rollback still `0e04af007375d82daf8f7d3b36c71abb0c784e0b`;
Windows, other projects, 7.8 and paid APIs remain untouched.

---

## Current gate — awaiting first scheduled diagnostic backup (20.11.2026)

**This is not yet PASS.** Exact reviewed source `infra/systemd/ae-backup.timer`
specifies `OnCalendar=*-*-* 03:00:00 UTC`, `RandomizedDelaySec=5min`,
`AccuracySec=1min`, `Persistent=true`.
The one-project diagnostic backup hook was installed and its first manual
snapshot verified by Netcup owner-admin [run 44726722286](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44726722286)
at 2026-11-20 11:21 UTC, **after** the daily 03:00 UTC event.
The next ordinary combined backup is therefore due **2026-11-21 about
03:00–03:06 UTC (05:00–05:06 Europe/Kyiv)** if the live timer retains the
reviewed schedule. No subsequent scheduled diagnostic snapshot has been
observed, so do not claim autonomous backup PASS.

**Read-only evidence needed after this time:** fresh
`systemctl show ae-backup.timer` / last trigger,
`ae-backup.service` success after the scheduled trigger,
a *new* verified diagnostic snapshot in
`/var/backups/ai-agent-engine-diagnostics`, plus existing controller and
three baseline units remaining healthy. Preserve the original backup files,
drop-in and all snapshots. Existing GitOps read-only status workflow checks
only three units and controller symlink, **not** backup chronology/private
snapshots. A narrowly reviewed authenticated metadata-only Netcup query will
be needed; do not pretend currently available status workflow proves it.
**No forced backup, deployment, service restart or private data mutation
was performed during this scheduling check.** The current gate remains
`PARTIAL / WAIT_FOR_SCHEDULED_TIMER`; do not skip to the real failure QA
without owner decision under the roadmap. Controller rollback stays
`0e04af007375d82daf8f7d3b36c71abb0c784e0b`.

---

## Latest verified — signed isolated diagnostics ACTIVE on Netcup, core unchanged

**Current source before this docs-only handoff update:** GitHub `main` `f1969a042d78f292302c1f189676fb8ce93ae4e7`.
**Prepared artifact:** exact reviewed source `60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23`, wheel `ai_agent_engine-0.3.19a0-py3-none-any.whl`, SHA-256
`86ee06520edcb68e5cc160a68822977fe3b27c7c1a412f58f92e653bbb6c3900`
(independently recalculated from downloaded run artifact; matches manifest).
[Preparation run 44737117973](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44737117973) **SUCCESS**.
[Unprivileged Netcup transfer run 44737411751](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44737411751) **SUCCESS**, including exact artifact and on-host SHA-256 verification.
**Owner explicitly confirmed service activation in chat.**
[Separately signed add-on activation run 44737886631](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44737886631) **SUCCESS**, server installer output
`MANAGED_DIAGNOSTICS_7_7.status=MANAGED_DIAGNOSTICS_ACTIVE` at 2026-11-20 12:41:49 UTC.
The service `ae-managed-diagnostics.service` was installed/enabled/started, restart probe **PASS**, local Unix socket probe **HOLD_EXPECTED**, owner PID/UID check **PASS**, 56 Python source files validated. It reported `baseline_services_unchanged=true`,
`old_controller_restarted=false`, `live_release_changed=false`,
`no_tasks_or_grants_created=true`, `model_or_network_enabled=false`.
Isolated project source pinned at `741fc1a4e62b52922611e1e28d4805d727dffe4d`.

**Existing controller/rollback reference:** package `0.3.11a0`, live symlink SHA
`0e04af007375d82daf8f7d3b36c71abb0c784e0b`; no core promotion.
The new diagnostic unit can be **stopped** independently with
`systemctl disable --now ae-managed-diagnostics.service` through the approved Netcup admin channel (not executed).
This is a stop procedure, **not** a verified full uninstall/rollback.
The previously active baseline `ae-controller.service`, `ae-backup.timer`,
`ae-host-check.timer` remained active during installer checks.

**Still required to finish 7.7:** observe a genuinely scheduled backup cycle;
conduct a controlled real managed FAILED → diagnostic incident → owner-verified QA
without synthesizing incidents or changing live customer/project data;
measure CPU, RAM and disk consumption during operation.
First diagnostic service lifecycle/socket verification is finished.
**7.7 PARTIAL; 7.8 HOLD.** Leave Windows and other projects untouched.
For the next step, verify the then-current `main`, service/core state and source
before any release or live test; no generic controller promotion.

---

# HANDOFF — 20.11.2026, AI Agent Engine 7.7 / signed add-on gate, live HOLD

## Latest — Netcup public signing trust installed, activation still HOLD

**Owner-approved exact host action:** GitHub `main` parent source
`f9e703d67a48aff1d354dc651eebfe2c046ec14a`, one immutable owner-admin
request `7b354439e5de1cb8393be71d9dc34842`, committed as
`2487cf95cb29fe5837670b4621aed318eb86ca61`.
The authenticated [Netcup owner-admin run 44736497987](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44736497987)
**SUCCESS**. Server evidence:
`DIAGNOSTIC_SIGNING_7_7.status=DIAGNOSTIC_SIGNING_BOOTSTRAPPED`,
`scope=vl-ktk/ai-agent-engine`, `signer_public_only=true`,
`restricted_addon_operator_installed=true`, `trusted_installer_pinned=true`,
`new_sudo_or_ssh_permissions=false`, `diagnostic_service_started=false`,
`core_release_changed=false`.

**Exactly installed root files:** `/etc/ai-agent-engine/release-signers`
(public key only, `0644`),
`/usr/local/libexec/ae-release/diagnostic_addon_operator.py` (`0444`),
`/usr/local/libexec/ae-release/activate_managed_diagnostics_7_7.py` (`0444`).
Bootstrap checked the reviewed SHA-256 of both Python files and exact file
modes after the restrictive admin `umask(077)`, checked the current release
and all three old baseline units before/after writes; existing files were
not overwritten. The prior and still-active controller version/rollback ID
is `0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`.
The previous no-Netcup signature validation
[run 44735340945](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44735340945)
also passed. No new service, socket, tasks, permissions or models activated.

**Next single step:** manually owner-dispatch the exact
`release-preparation.yml` for reviewed source
`60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23` under a freshly checked
`main`; capture full wheel SHA-256 and successful run ID, then separately
owner-dispatch the verified transfer, recheck Netcup core SHA and finally
signed add-on activation. Do not invoke generic core release promotion.
Live diagnostic restart/socket, scheduled backup observation, real
FAILED→incident→verified QA and CPU/RAM/disk measurements still remain.
**7.7 PARTIAL, 7.8 HOLD.**

## Previous — reviewed bootstrap umask fix, before Netcup installation

**Source baseline:** GitHub `main` `8a492158bb3c46f7bfe61d341d6bc8622720c189`
(recheck HEAD before any request); active Netcup core last observed
`0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`.
Owner-manual, no-VPS signature check [run 44735340945](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44735340945)
**PASS**, including exact public key match, OpenSSH Ed25519 signature and
verification; both VPS SSH/install steps **SKIPPED**.

**Safety fix awaiting source publication:** the authenticated owner-admin
runner sets `umask(077)`, so initial root file creation for modes `0644` and
`0444` would actually result in `0600` and `0400`. The additive bootstrap
`bootstrap_diagnostic_signing_7_7.py` now calls `fchmod` on the *same newly
opened descriptor* before checking the final inode; it tracks the newly
created path immediately for private rollback of partial writes. Eight
focused signing tests PASS including restrictive umask, no-clobber and
signature-rejection cases; full regression **895 OK (2 skipped)**,
source/workflow scans and compileall PASS.
**No root bootstrap, diagnostic unit, socket or model has been installed**.
Next: merge tested narrow fix, recheck fresh `main`/Netcup SHA, submit one
owner-admin bootstrap for public signer and trusted operator; then owner-only
manual release-preparation/transfer/signed activation. Do not use generic
controller promotion, Windows, other projects or paid APIs. 7.7 PARTIAL,
7.8 HOLD.

## Latest — isolated signed activation path, owner-approved, source preparation

**Exact source baseline for this change:** GitHub `vl-ktk/ai-agent-engine`
`main` `df70bbd9989d25f18a0079acb125abbecd934ed2`; recheck before PR,
request or deployment. **Last live Netcup SHA / rollback:**
`0e04af007375d82daf8f7d3b36c71abb0c784e0b`, installed core `0.3.11a0`.
Do not treat this older observation as a fresh host check.

**Owner scope:** only the approved AI Agent Engine diagnostic stage 7.7.
The owner approved a separate signing key and limited installation operator.
GitHub UI confirmed a distinct `AE_RELEASE_SIGNING_PRIVATE_KEY` secret;
the private key is not committed or printed. Matching public allowed-signer
is hard-pinned in the owner-admin bootstrap. The existing Netcup full owner
admin channel means **no new sudo/SSH rights are needed**.

**Prepared source files:**
- `infra/deploy/diagnostic_addon_operator.py`: root-only fixed add-on operator,
  strict signed intent with bounded freshness and separate operation name;
  source pinned to `60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23`, core live
  SHA pinned to `0e04af...`, exact wheel SHA, root-owned reviewed installer,
  verified root-private wheel snapshot. No core promotion or controller restart.
- `ops/admin-scripts/bootstrap_diagnostic_signing_7_7.py`: one-time owner-admin
  additive install of public signer, trusted add-on operator and previously
  reviewed diagnostics installer; creates no service/task, changes no sudoers.
- `.github/workflows/netcup-owner-diagnostics-activate.yml`: owner manual
  `check-signature` and `activate` modes; separate signing secret, pinned
  SSH host identity, existing release-preparation/transfer artifact, strict
  main/source/live SHA checks. The self-check is **read-only/no Netcup**.
- `tests/test_diagnostic_signing_gate_7_7.py` and workflow boundary rules;
  [`MANAGED_DIAGNOSTICS_7_7.md`](./infra/orchestrator/MANAGED_DIAGNOSTICS_7_7.md)
  describes exact safety limits.

**Offline verification of this candidate:** 7 targeted signing tests PASS;
full suite **894 OK (2 skipped)**; workflow policy scanner,
public-source scan, `compileall`, YAML parse and local wheel build
`0.3.19a0` PASS. **Separate private signing key self-test on GitHub,
root bootstrap and Netcup add-on activation have not run yet.**

**Next mandatory order, no shortcut:** test Actions secret signing and matching
public key with manual `check-signature` (64-zero wheel digest); only after
PASS publish a single reviewed owner-admin request to install public trust;
then owner-manual `release-preparation` for exact `60a53...`, transfer the
independently verified wheel, recheck `main` and Netcup active SHA, and run
the separate signed diagnostics activation with real wheel digest. Afterwards
test socket/restart and one-project safety, scheduled backup, true failure →
incident → verified QA and CPU/RAM/disk. Until actual Netcup checks,
**service not installed, 7.7 PARTIAL, 7.8 HOLD**. Never start the obsolete
standalone sidecar, update core/Windows/other projects or fake incidents.

## Previous source checkpoint — in-process diagnostic bridge, deployment HOLD

## 7.7 — single-process managed diagnostic candidate, SOURCE ONLY / activation HOLD

**Источник перед этой правкой:** GitHub `vl-ktk/ai-agent-engine` `main`
`50c593e03d9ac4956cddcd54dfaaa82abec41f5d`; source-revert только после
повторной сверки актуальной ветки. Последний действительно проверенный Netcup
active/rollback: `0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`.
В рамках этой source-only работы новой живой проверки Netcup нет.

**Подготовлено:** `ai_agent_engine/diagnostic_managed_runtime.py` держит один
approved TaskInbox (эксклюзивный Linux `flock`), один registry, закрытый
AF_UNIX socket и инертный `SingleProjectDiagnosticRunner` без grants и запуска
задач. Проверяет строгую private config, ноль незавершённых задач и точную
подготовленную Git-версию проекта `741fc1a4e62b52922611e1e28d4805d727dffe4d`.
Отдельный проверяемый кандидат systemd/installer:
`ops/admin-scripts/activate_managed_diagnostics_7_7.py`, протокол
[`MANAGED_DIAGNOSTICS_7_7.md`](./infra/orchestrator/MANAGED_DIAGNOSTICS_7_7.md).
Модуль pinned SHA-256
`dcf077701d2fd92fe6cef5cebcd8966e0fd3217c7c8c5a88eb3de447ec4e769b`.
Не заменяет текущий `/opt/ai-agent-engine/current`, controller, backup,
секреты/доступ и чужие проекты.

**Локальные проверки:** 11 новых offline tests PASS; полный регресс
**883 OK (2 skipped)**, `compileall`, public-source scan и workflow-boundary
scan PASS. Wheel из текущих исходников собран; это **не** согласованная живая
выкладка. Локальное AF_UNIX bind возвращает `EPERM`; настоящий socket/restart
QA остаётся Netcup-действием после release gate. GitHub CI отдельно проверить.

**Блокер запуска:** действующий протокол 7.7 требует ручной подписанный
owner-controlled release gate для нового runtime-кода. **Не создавать
`ops/admin-requests` для обхода этого порядка.** Нужно выбрать и подтвердить
способ signed deployment отдельного add-on либо узкий candidate через
существующие `release-preparation`/`transfer`/`activate`; нельзя слепо
продвигать весь `0.3.19a0` поверх live `0.3.11a0`. Ни installer, ни socket,
ни diagnostic service в этой итерации **не запускались**. 7.7 `[ ] PARTIAL`,
failure→incident→QA/overhead ещё не проверены, 7.8 HOLD.

## 7.7 — приватный диагностический бэкап на Netcup LIVE PASS

**Источник перед следующим шагом:** GitHub `main`
`47315de0deaf35528c95be55a37b7f2ea64fd7d1` (проверить HEAD перед
source revert); Netcup рабочий пакет остался `0.3.11a0`, active/rollback SHA
`0e04af007375d82daf8f7d3b36c71abb0c784e0b`.

**Фактически выполнено:** через source [PR #70](https://github.com/vl-ktk/ai-agent-engine/pull/70)
и аутентифицированный owner-admin [run 44726722286](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44726722286)
подключён отдельный one-project backup `/var/lib/ai-agent-engine-diagnostics`
→ `/var/backups/ai-agent-engine-diagnostics` к уже активному
`ae-backup.timer` через один обратимый `.service.d` drop-in (`ExecStartPost`).
Оригинальные `/usr/local/libexec/ae-backup.py`, `ae-backup.service`,
`ae-backup.timer` не заменены; новый диагностический сервис не запущен.
Сервер вернул `DIAGNOSTIC_BACKUP_ENABLED`, exact scope
`vl-ktk/ai-agent-engine`, снимок
`20261120T112114.457886Z-dac25d75`, `retained=1`, независимый
`verify PASS` и изолированный `RESTORE_SMOKE_PASS` трёх SQLite.
`systemd-analyze verify`, эффективный `ExecStartPost`, три базовых сервиса
и прежний live release проверены в том же owner-admin run. Код: [протокол](./infra/backup/DIAGNOSTIC_BACKUP_7_7.md),
12 новых disposable backup tests; локальный регресс **872 OK (2 skipped)**,
GitHub Ubuntu Python 3.11/3.12/3.13 и wheel PASS,
Windows CI FAIL без изменений Windows.

**Ограничение:** ещё не наблюдалось первое **автоматическое** выполнение
нового шага в следующем цикле `ae-backup.timer`; ручной первый снимок и
test-only restore действительно прошли. Это только локальные копии на том же
VPS, не off-host DR. Не перезапускать/перезаписывать старый backup service
и не удалять снимки для «проверки».

**Следующий ход в том же 7.7:** отдельный private Unix socket parent и ровно
один управляемый диагностический процесс, затем живые failure→incident→QA,
CPU/RAM/disk overhead, подтверждение сохранности старых сервисов; без
Windows/других проектов/платных API. 7.7 `[ ] PARTIAL / LIVE HOLD`, 7.8 HOLD.

## 7.7 — первый проект НА СЕРВЕРЕ УТВЕРЖДЁН, независимая проверка PASS

**Актуальная проверенная точка перед продолжением:** GitHub `main`
`bda7eb55644d2b16c762ae2ee66c39ad598aa1c0` (source rollback baseline
для следующей source-правки; всегда перечитать HEAD до отката).
Только для `vl-ktk/ai-agent-engine`, точный proposal
`proposal_8950a54bb02d4537943c5ed6f3286696`, digest
`8443ee2fd8de2cb0c893a39420b0229b463a055b014f3bf93c571952f573dd56`:

- Fresh [Netcup audit 44722400997](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44722400997)
  **SUCCESS**: runtime `0.3.11a0`, exact deployed rollback SHA
  `0e04af007375d82daf8f7d3b36c71abb0c784e0b`, baseline units active;
  host-wide CPU 0.0%, RAM 7.6%, диск 1.9% (не diagnostic overhead).
- Защищённый authenticated owner [registration 44722888936](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44722888936)
  **SUCCESS**: `APPROVED_INERT`, `approval_record_created=true`, leader SQLite
  v2, точный project scope, prepared Git `741fc1a4e62b52922611e1e28d4805d727dffe4d`,
  `service_activated=false`.
- [Independent read-only verification 44724070020](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44724070020)
  **SUCCESS**: `APPROVAL_VERIFIED_READ_ONLY`, exact registry/leader identity,
  schema/app IDs/SQLite integrity, zero tasks, immutable prepared Git checkout,
  no diagnostic socket, existing baseline services active. Открыт только
  `private_host_directories` (пока нет socket parent). Новый скрипт и тесты
  опубликованы через PR #66/#67; `860 unittest OK (2 skipped)` локально,
  столько же проверено на GitHub owner-admin Ubuntu. Windows CI отдельно FAIL;
  Windows не меняли.

**Что осталось в том же 7.7:** backup coverage *новой* частной ветки
`/var/lib/ai-agent-engine-diagnostics` (нынешний `ae-backup.py` копирует только
`/var/lib/ai-agent-engine`), проверка изолированного восстановления,
private socket/один runtime, живой failure→incident→verified QA,
CPU/RAM/disk overhead. Диагностику не запускать до backup/QA gates, другие
проекты/секреты не менять; 7.7 `[ ] PARTIAL / LIVE HOLD`, 7.8 не начинать.

## Текущий шаг после переноса чата — owner proposal / offline registration gate

**Исходная ветка для этого изменения:** GitHub `vl-ktk/ai-agent-engine` `main`
`2d53c804adba6de3dd54bd432c36b31c3e1fec04`. Следующий шаг — получить
внешнее аутентифицированное подтверждение **точной** заявки
`proposal_8950a54bb02d4537943c5ed6f3286696`, SHA-256
`8443ee2fd8de2cb0c893a39420b0229b463a055b014f3bf93c571952f573dd56`.
Ссылка на предложение и процедуру: [HOST_OWNER_APPROVAL_7_7.md](./infra/orchestrator/HOST_OWNER_APPROVAL_7_7.md).

**Подготовлено только в исходнике:** жёстко закреплённый payload, новый
однопроектный `approve_single_project_7_7.py` для **уже созданного** приватного
каталога Netcup и offline tests. Он не стартует сервис, не создаёт задачи,
grants, сокет и не подключает модели. Owner-admin JSON **не создавался**,
workflow **не запускался**, запись в реальный project registry **не внесена**.
Digest owner-admin script:
`59d4c99aebbd7b0c611a33ffbe27e126216f4ccf4c65645645df45b4b8bfd742`.
Проверено локально на текущих исходниках: **8 новых targeted PASS**,
**855 unittest OK (2 skipped)**, `compileall`, public-source scan,
workflow-boundary scan, staged diff и wheel `0.3.19a0` **PASS**. GitHub CI
для исходной правки [run 44720757706](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44720757706):
Ubuntu Python 3.11/3.12/3.13 и wheel **PASS**; Windows **FAIL**
(`fcntl` и один дополнительный `AssertionError`; Windows не меняли). PR
[#62](https://github.com/vl-ktk/ai-agent-engine/pull/62) объединён в
`main` **`f217d100a70672e64966fe83f3d9c14a13d47c8f`**.
Предыдущий source SHA для адресного отката — **`2d53c804adba6de3dd54bd432c36b31c3e1fec04`**;
откат исходника только после проверки текущего `main` через `git revert`
в GitHub без force-push. Изменений Netcup/Windows и админ-запросов не было.
Актуальная live Netcup версия до нового read-only подтверждения — прежний
зафиксированный `0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`.
Разрешение на новый host write и точное owner approval нужно получить отдельно.

**Статус 7.7:** `[ ] PARTIAL / LIVE HOLD`. Следом после подтверждения заявки:
fresh GitHub/Netcup checks, owner-admin scoped registration, backup coverage,
private socket, один управляемый runtime, затем E2E failure→incident→verified QA
и реальные CPU/RAM/disk замеры. Windows/другие проекты/платные API не изменять;
7.8 не начинать.

## Подтверждено: первый приватный проект на Netcup подготовлен, сервис не запущен

**Реальный результат 20.11.2026:** опубликованы [исходники provisioning](https://github.com/vl-ktk/ai-agent-engine/commit/741fc1a4e62b52922611e1e28d4805d727dffe4d) и отдельный immutable owner-admin request `f31b7ab510cb2141a4170a7cc775c36b` в GitHub `main` **`578c1406cbfa667363075b43e57f441fb1ced832`**. [Netcup owner-admin run 44713790672](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44713790672) завершился **SUCCESS**, журнал host вернул `PROVISION_7_7.status=PREPARED_INERT`, `schema=1`, `task_schema=14`, `registry_schema=1`, `scope=vl-ktk/ai-agent-engine`, `source_commit=741fc1a4e62b52922611e1e28d4805d727dffe4d`. Это подтверждает фактическое создание выделенного приватного пространства `/var/lib/ai-agent-engine-diagnostics`, пустой TaskInbox v14, пустого project registry v1, `runtime.json` и checkout проверенного Git SHA. Новая структура принадлежит `ae-runtime` и проверяется на private permissions; существующее рабочее хранилище не мигрировали.

**Безопасные границы:** `owner_approval_created=false`, `service_activated=false`, readiness **`HOLD_OWNER_APPROVAL_AND_RUNTIME_ACCEPTANCE`**. Не создавали поддельное `OwnerDecision`, реальную project-leader DB v2, задачи, grants, новый unit/socket, runtime model, не включали платные API. Live active release **остался `0e04af007375d82daf8f7d3b36c71abb0c784e0b`**, установленный пакет **`0.3.11a0`**; это исходный checkpoint для отката при *будущем* package deployment, который в этом шаге не потребовался. На Windows и других проектах ничего не меняли. Административный GitHub-транспорт, как обычно, сохранил приватные job-evidence отдельно в `/root/ae-admin-jobs`.

**Проверено:** локально **847 тестов PASS (2 skipped)**, `compileall`, public-source scan, workflow-boundary scan, wheel `0.3.19a0`, staged diff PASS; GitHub [Linux CI run 44713514156](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44713514156) Python 3.11/3.12/3.13 и wheel **PASS**. Общий CI не зелёный только из-за прежней Windows-only ошибки `fcntl` — **не исправляли Windows**. Живой owner-admin provisioning PASS, source SHA совпал, новый service **не был запущен**. Не считать это проверкой работающей диагностики или фактического CPU/RAM/disk overhead 7.7.

**Следующий отдельный шаг:** через доверенный интерфейс получить действительно аутентифицированное одобрение владельцем точного `ProjectProposal` (id/hash), затем project-leader store v2, backup coverage, необходимые приватные пути сокета и один runtime-процесс; лишь после этого отдельная live QA failure→incident→verified result, измерение ресурсов, повторная проверка live SHA и owner-controlled release. Без новой проверки/разрешения сервис не активировать. **7.7 `[ ] PARTIAL / LIVE HOLD`; 7.8 не начинать.** Предыдущие разделы ниже описывают последовательность подготовки и аудита до этого выполненного шага.

## Актуальный шаг — строго однопроектный private provisioning, без activation

**Разрешение:** после live host audit владелец 20.11.2026 сказал «Хорошо, давай» на подготовку первого проекта Netcup. Исходник GitHub `main` **`ef276a190a495145e08921f44fe27ff91dbd7bf4`** (точный source baseline для возможного адресного revert новой правки после проверки HEAD). Live Netcup до шага: пакет `0.3.11a0`, immutable SHA **`0e04af007375d82daf8f7d3b36c71abb0c784e0b`** по [admin audit 44711570153](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44711570153); это checkpoint для отказа/отката, не вечное утверждение.

**Подготовленный исходник:** [`provision_single_project_7_7.py`](./ops/admin-scripts/provision_single_project_7_7.py), [протокол](./infra/orchestrator/HOST_PROVISIONING_7_7.md), [офлайн тесты](./tests/test_netcup_single_project_provision.py). Строго при отсутствующем target создаёт приватную root-структуру `/var/lib/ai-agent-engine-diagnostics` и `runtime.json`, пустые task SQLite v14/registry v1, реальный отдельный Git checkout SHA-reviewed source, затем передаёт весь новый каталог `ae-runtime` (0700 dirs/0600 data) и перепроверяет чистый source и schema. Никаких `OwnerDecision`, project leader DB/approval, grants, задач, models, новых unit/socket, service restart, package activation или других проектов. При ошибке **не удаляет** новый каталог, а перемещает его в приватный recovery артефакт GitHub owner-admin job; существующий target никогда не переписывает. После первой приёмки без authenticated owner decision host preflight должен оставаться **HOLD**.

**Процедура:** после локальных и Linux CI проверок сделать отдельный one-file `ops/admin-requests` с immutable parent SHA, fresh expected Netcup release, точным script SHA-256 и owner-actor gate. Административный канал самостоятельно архивирует source в `/root/ae-admin-jobs` и не трогает существующую data storage. После реального результата дописать run ID/блокеры сюда; до того live provisioning **не утверждать**. Backup coverage новой директории, реальный authenticated owner approval, production TaskInbox singleton, Unix socket parent и live QA/замеры overhead — **открытые зависимости**. Windows/другие проекты/секреты не менять, 7.7 `[ ] PARTIAL / LIVE HOLD`, 7.8 не начинать.

**Проверки текущих исходников:** 5 новых disposable tests включая no-overwrite, recovery, symlink refusal и fail-closed preflight; полный `unittest discover -s tests -q` **847 OK (2 skipped)**; `compileall`, public-source scan, workflow-boundary scan и staged diff **PASS**; локальный wheel `0.3.19a0` собран. SHA-256 проверяемого owner-admin скрипта **`8edfc99c9b5d30b12311272260c38158e60c1b87e3f20da0ce4543a3656e3f05`**. GitHub CI и host execution проверять отдельно после публикации; не приписывать им локальные результаты.

## Живой Netcup-аудит 7.7 выполнен — дальнейшая инфраструктура HOLD

**Источник перед этой записью:** GitHub `main` `dc76436bf6b57e6e7938d13749aa6c74c1fad7d2` (точный SHA предыдущего состояния для source-revert; только после проверки HEAD, без force-push). Новый однопроектный административный запрос `082f447a691e96d500ff94010c0859dc` через штатный owner-admin workflow [44711570153](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44711570153) — **LIVE PASS**. Пinned source `a9cbb63eeaef62ba0b0679c46bf5556bdb61ff8c`, script digest `59e19500a90445cf4ecd6255d4aace0760c67eacdd72b9e6b101c3344061ff3c`. Админ-канал выполнил полный offline regression и запустил **только read-only host audit**; его технический транспорт сохранил приватный source/evidence в `/root/ae-admin-jobs/<request_id>`. Не было deployment, restart, миграции, новых сервисов, grant или доступа к secret contents. Windows не меняли.

**Факты с VPS:** установленный пакет **`0.3.11a0`**, current release/rollback baseline **`0e04af007375d82daf8f7d3b36c71abb0c784e0b`**, `ae-controller.service`, `ae-backup.timer`, `ae-host-check.timer` активны. В **предлагаемых** путях `/var/lib/ai-agent-engine-diagnostics/{projects,workspaces,tasks.sqlite3}`, project registry и `/etc/ai-agent-engine/diagnostic-runtime.json` — `missing`. Это не доказывает, что похожие файлы отсутствуют во всех возможных каталогах VPS, но конкретный approved runtime для 7.7 на этих путях **не provisioned**. Отдельной authenticated owner approval, реальной TaskInbox runtime identity и live diagnostic processing нет доказательств. **7.7 `[ ] PARTIAL / LIVE HOLD`; 7.8 не начинать**.

**Краткий замер хоста в момент аудита:** CPU busy **0.0%** (короткий сэмпл), RAM used **7.5%**, filesystem used **1.9%**. Это **весь хост**, не доля нашего AI-ядра и не прогноз нагрузки будущего 7.7. Долговременные данные CPU/IO/дисковой скорости и собственный budget/retention не измерены; не выдавать цифры о влиянии диагностики до её контролируемого запуска.

**Проверки:** исходники owner-admin script + 5 offline tests; весь локальный регресс **842 OK (2 skipped)**; compileall, public-source, workflow boundaries, wheel PASS. GitHub Linux Python 3.11/3.12/3.13 + wheel [run 44711290442](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44711290442) PASS; Windows job FAIL по `fcntl` — вне задачи. Новый root-admin job PASS, GitHub CI для request commit имеет общий FAIL из-за Windows, не считать весь matrix зелёным. В результате Netcup **не была активирована** новая версия: target remains `0.3.11a0`.

**Следующий безопасный шаг внутри 7.7:** отдельный, точный проектный provisioning для `vl-ktk/ai-agent-engine` (корневая runtime-identity, приватные каталоги/registry/task DB и доверенное owner-approved решение, без выдуманного approval), подготовка контролируемой one-process unit + закрытого сквозного failure→incident QA. Перед каждым host mutation сверить live SHA с `0e04af0...`, сделать snapshot/rollback, запрещать изменение других проектов/Windows, не обходить owner-only GitHub gates. Перед activation повторить live status и построить проверенный immutable wheel/release candidate. Не запускать параллельных TaskInbox писателей или obsolete diagnostic sidecar; не включать платные AI и ресурсоёмкий архив автоматически.

## Следующий шаг 7.7 — owner-admin аудит существующего Netcup, без активации

**Разрешение:** 20.11.2026 владелец сказал «Давай, даю все разрешения» на продолжение подключения 7.7. Ограничение одного проекта `vl-ktk/ai-agent-engine`, защита рабочих данных и штатные owner-only release gates сохраняются. GitHub исходная ветка до новой проверки: `53536e9ff58745ffbd7073ecd347f975e06469e5`. После разрешения выполнена новая **read-only** GitOps проверка [44710573295](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44710573295) — **PASS**; сервисы `ae-controller.service`, `ae-backup.timer`, `ae-host-check.timer` активны, current release SHA **`0e04af007375d82daf8f7d3b36c71abb0c784e0b`** (этот же SHA — точный baseline rollback до будущей выкладки). Запрос-источник `c053baddf1fbab8bbeaa775363e12f3381892b73`. Это проверка статуса, **не** proof готового 7.7.

**Текущая работа:** безопасный отдельный [`diagnostic_host_audit_7_7.py`](./ops/admin-scripts/diagnostic_host_audit_7_7.py) для уже существующего GitHub owner-admin канала. Только read-only status/proposed private path metadata, версия пакета и краткий host-wide CPU/RAM/disk snapshot; без чтения секретов и данных SQLite, произвольных команд, создания grant/approval, деплоя, новых systemd служб, перезапусков, очистки и платных API. Административный канал всё же сохраняет свой проверенный immutable source/evidence в приватном `/root/ae-admin-jobs` — отличать это от полного отсутствия host writes. Snapshot **не** означает overhead диагностического ядра и не доказывает, что оно подключено. При несовпадении SHA/правах/сервисах — STOP. Сначала опубликовать и проверить код, затем отправить **отдельный** owner-admin request с pin `source_sha`, `expected_current` и SHA-256 скрипта. Проверить ответ GitHub Actions и сохранить реальные blockers. Ни Windows, ни другие проекты не трогать.

**Локальные проверки этого кода:** 5 новых offline тестов `PASS`; общий `unittest discover` **842 OK (2 skipped)**, `compileall`, public-source scan, workflow-boundary scan, staged diff PASS. Локальный wheel `0.3.19a0` собран, содержит диагностические модули (админ-скрипт остаётся в исходнике, а не в установленном runtime wheel). SHA-256 **админ-скрипта** `59e19500a90445cf4ecd6255d4aace0760c67eacdd72b9e6b101c3344061ff3c` — pin для последующего immutable owner GitHub request. GitHub CI/живой admin-аудит подтвердить отдельно, не объявлять заранее.

**Статус:** 7.7 `[ ] PARTIAL / LIVE HOLD`; исходник `0.3.19a0` есть в GitHub, полноценный host task runtime/owner approval и live QA ещё не подтверждены. Перед реальным activation заново сверить live release SHA, точный candidate+wheel digest, rollback и выполнить проверенные owner-only release workflows. Не начинать 7.8.

## Актуальный шаг — read-only проверка готовности host, source-only `0.3.19a0`

**Задача/границы:** по «Давай» подготовить следующий безопасный шаг 7.7 для **единственного** проекта `vl-ktk/ai-agent-engine`; не трогать Netcup, Windows, секреты/права, существующие БД, сторонние проекты, платные сервисы и GPT/Claude. Источник до изменения: GitHub `main` `c15b2b9f71572001012ada42542aff066f6e54fa` (для адресного source-revert после проверки HEAD). Исторический Netcup rollback SHA `0e04af007375d82daf8f7d3b36c71abb0c784e0b` / пакет `0.3.11a0` — **не свежий** live замер.

**Сделано в источнике:** [`diagnostic_host_preflight.py`](./ai_agent_engine/diagnostic_host_preflight.py) и [операторский протокол](./infra/orchestrator/HOST_PREFLIGHT_7_7.md). Отдельный CLI-флаг `--config` осматривает **только существующие** 0600/0700 host config/каталоги, registry v1, project SQLite v2, task SQLite v14 под безопасным read-only режимом/неблокирующим shared lock, scope, неактивность очереди и чистую локальную Git-версию; при недостаточном/неоднозначном состоянии — HOLD с ограниченными кодами. Не создаёт ничего, не запускает процессы/сервисы и не выдаёт owner approval. Проверка *внутренней согласованности* approval-row — не внешняя аутентификация владельца. На Netcup CLI **не запускался**; 7.7 **`[ ] PARTIAL / LIVE HOLD`**, 7.8 не начинать.

**Проверки исходника:** 8 новых offline тестов; полный `python -m unittest discover -s tests -q` — **837 OK (2 skipped)**; `compileall`, `check_public_source.py`, `check_workflow_boundaries.py`, `git diff --cached --check` — **PASS**. Wheel `ai_agent_engine-0.3.19a0-py3-none-any.whl` собран, содержит `diagnostic_host_preflight.py`, SHA-256 `05f2769950936e587831e25ded931edc25b524190ce9eaf4cc2fbcb612c9d1fd`. GitHub source commit/CI проверять после публикации; local tests не доказывают Netcup host readiness.

**Осталось:** owner-validated preprovisioning ровно одного проекта, допустимые runtime-grants и выбранный host process/unit, fresh read-only Netcup release SHA непосредственно перед будущим owner-controlled release, безопасный candidate/rollback, ручные release-preparation/transfer/activate, live QA и измерения CPU/RAM/disk. Отдельное согласие на server mutation обязательно; ресурсоёмкие архив/retention/cleanup пока открыты.

## Текущий шаг — первый настоящий однопроектный offline task runner, `0.3.18a0`

**Источник до начала:** GitHub `vl-ktk/ai-agent-engine`, `main` `c8ba64d576c24eb46ae12bbd60ec700747fa79d7` (для адресного отката новой source-правки; перед `git revert` всегда снова проверить HEAD). Последняя подтверждённая live Netcup версия из прежнего read-only статуса `0.3.11a0`, SHA `0e04af007375d82daf8f7d3b36c71abb0c784e0b`; это **не** новый замер. Windows и Netcup не меняли.

**Добавлено в исходник:** `ai_agent_engine/single_project_diagnostic_runner.py`, тесты и [протокол](./infra/orchestrator/SINGLE_PROJECT_RUNNER_7_7.md). `SingleProjectDiagnosticRunner` использует только **существующий** открытый exclusive `TaskInbox`, approved `ProjectLeaderStore`, private `0600` config и `TaskPermissionGate` в том же процессе; допускает ровно `vl-ktk/ai-agent-engine`. Ровно один runtime на один открытый inbox, конкурентные `run_next` запрещены. `run_next()` выбирает одну уже допущенную QUEUED задачу по приоритету, preflight Git baseline/dirty version, закрепляет workspace и запускает **ровно одну фиксированную read-only** команду `git diff --check` через `TaskProcessController.launch_gated`; разрешение `EXECUTE_TOOL` записывается в доверенный ledger перед разблокировкой дочернего процесса. Нет пользовательского shell/argv, интерпретации prompt, сетевых команд, моделей, новых grants, платных API, произвольных тестов или нового сервиса. Успешная реальная проверка → durable `SUCCEEDED`/delivery ACK; реальный Git check nonzero с идентичной source version → один `FAILED`/delivery exhausted и диагностический incident; неизвестность, отказ гранта, изменение Git, timeout и ранее активные процессы → HOLD/без auto retry. Два SQLite по-прежнему не атомарны; при сбое diagnostic write `FAILED` остаётся и повторять можно только evidence с проверенной версией, не действие.

**Сопутствующая безопасная правка** `TaskProcessController.launch_gated`: optional `before_release` исполняется после durable `RUNNING` и перед разрешением дочернему `exec`; при отказе child не выполняет код, команда отменяется. `quiet` подавляет stdout и наследование stdin только у нового ограниченного процесса; прежние вызовы без флагов работают без изменений. Новый шаг **не** включает реальный GPT/Claude task runtime/автоматическую бесконечную очередь и **не** является live 7.7. Нет пользовательского owner-auth канала, preprovisioned Netcup SQLite/release, host замеров/QA.

**Проверки локального исходника `0.3.18a0`:** 8 новых Linux/offline тестов runner, ещё 1 тест pre-release gate, полный `829 unittest OK (2 skipped)`; целевые `task_control` 21 PASS, `single_project_diagnostic_runner` 8 PASS; `compileall`, source scan, workflow boundary scan, `git diff --check` PASS. Wheel `ai_agent_engine-0.3.18a0-py3-none-any.whl` SHA-256 `5f9e6bbcd7c783344a37b264223ff72691748a9ed28d203c9595b41917da8017`, содержит новый модуль. Источник предыдущей версии `0.3.17a0` [GitHub Actions 44706436410](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44706436410): Linux Python 3.11/3.12/3.13 + wheel **PASS**, Windows **FAIL** по старому `fcntl`; Windows не исправлять. Новый CI проверить отдельно после публикации.

**Осталось:** 7.7 **`[ ] PARTIAL / LIVE HOLD`**. Нет установленного на Netcup однопроектного TaskInbox/runtime и утверждённой host identity/config/grant политикой с owner approval; текущая проверка — фиксированная Git, не общий AI worker. Следующее разрешённое направление — спроектировать host provisioning/owner-approval/release candidate и отдельно получить разрешение на изменение Netcup; непосредственно перед выкладкой сверить текущий Netcup SHA, проверки и rollback, использовать ручные owner GitHub workflows. Не изменять Netcup/Windows, рабочие данные, секреты, права и другие проекты; не переходить к 7.8, не включать фоновые сборщики или платные API без согласия.

## Текущий шаг — реальный process-exit → FAILED → диагностика, source-only `0.3.17a0`

**Что подготовлено:** только в GitHub-исходнике `ai_agent_engine/project_diagnostic_adapter.py` добавлен `TaskDiagnosticAdapter.finish_managed_process(...)`, который работает с **тем же живым** `TaskInbox` через уже существующий доверенный `TaskProcessController`. Успешное завершение не создаёт дефект; при фактически завершившемся изолированном child process и доказанном nonzero exit code контроллер атомарно фиксирует `FAILED`+audit в своей task SQLite, затем диагностический адаптер проверяет worker/assignment, версию Git и сохраняет bounded/idempotent incident в отдельную project SQLite. Перед terminal state выполняется проверка scope/source/baseline. Caller обязан сохранить Git baseline *до* выполнения процесса, а не доверять SHA от модели. Никаких моделей, новых грантов, планировщиков или серверных запусков эта функция не добавляет.

**Транзакционная граница:** две SQLite не образуют общую атомарную транзакцию. Если запись incident после `FAILED` не состоялась, процесс **не перезапускать**; доверенный owner может позднее повторить только `record_failed_task(...)` с теми же сохранёнными аргументами. Если они потеряны после crash — HOLD/ручная проверка. Полноценного task runner/автопоставщика событий `FAILED` на Netcup по-прежнему нет.

**Проверки кандидата:** 4 новых offline-теста настоящих завершившихся дочерних процессов: real nonzero→FAILED+incident, zero→SUCCEEDED без инцидента и отказ от ложного статуса, запрет чужой identity/Git, отсутствие replay после сбоя записи диагностики; `18` тестов адаптера, `1 skipped` из-за AF_UNIX sandbox. Общий регресс **820 OK, 2 skipped**; `compileall`, `check_public_source.py`, `check_workflow_boundaries.py`, `git diff --check` — PASS. Wheel `0.3.17a0` собран: SHA-256 `4cda80d81299b5fd16d49b4d1401412e49112aaf7de501f05a9f4d9fe9c5f811`, архив содержит все три диагностических модуля. **Исходный HEAD до шага:** `88a8bcd4605f8d9e1a2e6d2682ea8a4feee8de1d`; адресный `git revert` нового feature-коммита после проверки актуального `main`, без force-push. Netcup source/host не менялся — серверный rollback не нужен.

**Предыдущая Linux CI проблема `0.3.16a3` подтверждена исправленной:** [run 44705246327](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44705246327) — Ubuntu Python 3.11/3.12/3.13 и wheel **PASS**, реальный Unix socket тест в Ubuntu PASS; Windows отдельно **FAIL** по прежнему Linux-only `fcntl`, не менять. Для новой версии CI после публикации проверить отдельно. **Статус 7.7 `[ ] PARTIAL / LIVE HOLD`**, scope только `vl-ktk/ai-agent-engine`; никаких Windows/Netcup изменений, расходов, новых прав, секретов, рабочих данных, других проектов. Не начинать 7.8. **Следующее:** owner-approved live TaskInbox runtime и project registry, ручной GitHub owner-controlled release prepare/transfer/activate, свежий read-only SHA сервера и live QA/CPU/RAM/disk до закрытия 7.7. Последний известный Netcup release `0.3.11a0`, SHA `0e04af007375d82daf8f7d3b36c71abb0c784e0b` (не свежий замер; перепроверить перед любой серверной правкой).

## Текущая работа — единый процесс TaskInbox + диагностика, source-only `0.3.16a3`

**Подтверждённая ошибка Python-кода и исправление `0.3.16a3`:** при удалении старого stand-alone пути был по ошибке удалён `from contextlib import closing`, а `serve_local_intake` по-прежнему использовал `with closing(conn)` сразу после `accept`. Это вызывало `NameError` в сокетном потоке, соединение не получало ответ; Linux CI отображал `TimeoutError`. Импорт **восстановлен**, временная CI-трассировка из теста удалена. Вторая версия `0.3.16a1` не устраняла причину; версия `0.3.16a2` была только диагностической, не live исправлением. Исходный SHA перед исправлением `f544636ca9ac49128531a82415b7d1dabc28928e`. **Подтвердить Linux socket CI после публикации `0.3.16a3` прежде, чем считать источник исправленным.** Netcup/Windows не затрагивать.

**Локальные проверки `0.3.16a3`:** 31 тест 7.7 PASS (1 skipped), полный `816 unittest OK (2 skipped)`; `compileall`, workflow boundaries, public-source scan, Git diff PASS. Добавлен локально исполняемый тест контекстного менеджера для cleanup сокета (без AF_UNIX), чтобы пропущенный импорт снова не прошёл незамеченным. Wheel `ai_agent_engine-0.3.16a3-py3-none-any.whl` собран, SHA-256 `c0b75082a6e245804bc35ddb980f3fe5fbf68645c6f8deb0a8c0949cac61e053`. AF_UNIX настоящий E2E выполняется только в GitHub Ubuntu CI, исход пока не подтверждён.

**CI диагностика `0.3.16a2` (временная):** повторный Ubuntu CI `0.3.16a1` [run 44704227901](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44704227901) опять **FAIL** в том же socket timeout (3 s) несмотря на binding на owner thread. Это **не** подтверждает причину, только наблюдение. Добавлена безопасная печать имён функций/строк текущего сокетного потока **лишь при timeout**, без raw events, токенов или путей. Следующая публикация — измерительно-диагностическая, **не** исправление; до выяснения причины нельзя считать Linux CI и 7.7 PASS. SHA перед диагностической публикацией `88919cbb769fc5959c487a3673981e19e2158494` для точечного отката.

**CI correction:** первая публикация этой привязки `0.3.16a0` (`8bb65d7f2158d560f35e6c1d5b556d431c08798e`) прошла wheel/Ansible checks, но GitHub Ubuntu Python 3.11/3.12/3.13 **FAIL** в единственном AF_UNIX-тесте (socket read timeout 3 s; [CI run 44703465223](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44703465223)); локальная среда этот socket test пропускает из-за sandbox EPERM. Исправление `0.3.16a1`: доверенную binding-конфигурацию создавать на owner thread **до** запуска socket thread, а затем использовать готовый in-process адаптер в уже проверенном socket loop. Проверка CI новой версии ещё должна подтвердить результат; не объявлять E2E PASS заранее. Windows по прежнему Linux-only `fcntl` FAIL, не менять.

**Проверки `0.3.16a1` перед GitHub CI:** целевые 30 unittest OK (1 skipped), полный регресс 815 unittest OK (2 skipped), `compileall`, workflow-boundary и public-source scans, staged diff PASS; wheel `ai_agent_engine-0.3.16a1-py3-none-any.whl` собран, SHA-256 `7ad23baeae700c3c2ad1d4f727f4477a4941abb20c41a498ca33f59035fadc10`. Unix-socket E2E **локально не выполнялся** из-за EPERM; исход первой попытки CI `0.3.16a0` — не PASS. Текущий опубликованный GitHub source перед hotfix `8bb65d7f2158d560f35e6c1d5b556d431c08798e` — адресный исходный коммит для отката hotfix.

После «Давай» владельца подготовлен безопасный **in-process** способ подключения диагностики 7.7 к **уже открытому** единственному `TaskInbox`: `bind_inprocess_diagnostics(config, inbox=..., scope=..., leaders=...)` возвращает адаптер в том же процессе; `run_configured_service(..., inbox=..., scope=..., leaders=...)` принимает локальные события по сокету, не открывая вторую task SQLite или новый registry. Валидация сверяет одобренный owner/project, фактический inode открытого task DB, private config `0600`, идентичность workspace и project-leader roots, ровно один источник Git; чужая область, неподтверждённая конфигурация и закрытая task DB получают HOLD. Автономный вызов CLI без владения существующей очередью **запрещён** (`standalone_task_owner_forbidden`); устаревший systemd sidecar остаётся только source-only и без рестарта. Приватные БД и approval без явного владельца не создаются, модель/инструменты/новые права не запускаются.

**Состояние:** GitHub parent source SHA `c04a26684454e18ea56c6c26e8f06480982e881f` (для адресного отката этого feature-commit); Netcup по последней независимой read-only проверке `0.3.11a0`, SHA `0e04af007375d82daf8f7d3b36c71abb0c784e0b` (перед любой выкладкой пересмотреть). **Нет** запущенного на Netcup owner task runtime, производящего подтверждённые `FAILED` события, и нет подтверждённого owner-approved project registry. Ручной GitHub owner workflow обязателен после проверки готовности. **Netcup/Windows/секреты/права/другие проекты не менять, 7.7 [ ] PARTIAL / LIVE HOLD; 7.8 не начинать.** **Проверки:** 30 тестов 7.7 PASS (1 skipped: AF_UNIX sandbox EPERM); полный `unittest` **815 test cases OK, 2 skipped**; `compileall`, workflow-boundary scanner PASS; wheel `ai_agent_engine-0.3.16a0-py3-none-any.whl` собран, содержит 7.7-модули, SHA-256 `b59ef0498dc3e16213835ffcfed1e51cf362060a0afb5b682b35a905060b2246`. Отдельный CLI отказал со статусом 1, как и задумано. GitHub CI и Netcup E2E — только после публикации/подключения.

## Последнее решение владельца — сначала только AI Agent Engine

Владелец **согласовал подключение только проекта AI Agent Engine**; никакие другие проекты включать или наделять доступом нельзя. Для первого проекта зафиксированы допустимые `OwnerScope` идентификаторы: `owner_id=vl-ktk`, `project_id=ai-agent-engine`. Это **внутренние идентификаторы**, а не новые GitHub-аккаунты, учетные данные или доказательство аутентификации. Первый runtime `0.3.15a0` fail-closed требует **ровно один project_id** и совпадающий source binding; набор из двух проектов отклоняется. Образец без секретов: [single-project config](./infra/orchestrator/diagnostic_runtime_7_7.single-project.example.json), подробности — [runtime 7.7](./infra/orchestrator/DIAGNOSTIC_RUNTIME_7_7.md).

**Публикация — только GitHub-исходники, не сервер:** новая конфигурация представляет рекомендованные пути **для будущего owner-reviewed provisioning**, которых пока никто не подтвердил на VPS. Проектный реестр 7.5 и task SQLite v14 ещё не созданы/не подтверждены на Netcup, отсутствует реальный производитель `FAILED` событий в том же единственном TaskInbox-процессе. Нельзя брать это JSON как фактическую работающую конфигурацию и нельзя автоматически создавать `OwnerDecision`. User-side GitHub release workflows `prepare`, `transfer`, `activate` требуют отдельного ручного dispatch от `vl-ktk`; подключённый браузер для выполнения этого действия недоступен. **Netcup и Windows не изменены**, сервис не установлен; 7.7 **[ ] PARTIAL / DEPLOYMENT HOLD**. Последняя подтверждённая read-only серверная версия `0.3.11a0`, SHA `0e04af007375d82daf8f7d3b36c71abb0c784e0b`; перед выпуском обязательно пересмотреть live SHA. Предыдущий GitHub source SHA для адресного отката: `78b8b59e3f45704913f3fdf122d1c7971c1f9f8e`. **Проверки текущей source-правки:** `28 unittest OK (1 skipped)` в 7.7-поднаборе; полный `813 unittest OK (2 skipped)`; `compileall`, `check_public_source.py`, `check_workflow_boundaries.py`, staged diff — PASS; wheel `ai_agent_engine-0.3.15a0-py3-none-any.whl` собран, SHA-256 `c48be3941237e9f65b2c1d667af8512cacf28ddeec34bc77fc491628714f7d6c`. Unix-socket E2E в данной среде не выполнен; GitHub Actions/Netcup runtime отдельно не подтверждены.

## Последнее решение — разрешён отдельный серверный обработчик и приватная диагностика

Владелец ответил «Давай» на явный вопрос о расширении 7.7 до отдельного серверного процесса и закрытой SQLite **без** автоматических GPT/Claude и платных API. Windows не изменять; изменение существующих секретов/прав доступа и обход штатных owner release workflows не разрешались.

**Подготовленные исходники `0.3.14a0`, НЕ server deployment:**
`ai_agent_engine/diagnostic_intake_service.py`, дополнительный Linux test coverage в `tests/test_project_diagnostic_adapter.py`, source-only `infra/systemd/ae-diagnostic-intake.service.template`, [протокол](./infra/orchestrator/DIAGNOSTIC_RUNTIME_7_7.md). Строгий лимит события 2048 bytes, `AF_UNIX` + peer UID, приватный сокет 0600 в 0700 каталоге, один запрос на соединение, без сети/моделей/shell/новых грантов. Требуется отдельная owner-approved конфигурация и проектный реестр, подготовленный task SQLite точной схемы v14 (без неявной миграции). Используется только уже опубликованный `TaskDiagnosticAdapter` с проверенной задачей, worker и Git baseline, HOLD на недостоверном сигнале, recovery без replay.

**Критическая граница:** это *приём подтверждённых событий*, **не** полный исполнитель задач. `SQLiteTaskStore` — строго единоличный писатель с `flock`; запуск sidecar против действующей очереди запрещён. На Netcup отсутствует доверенный постоянно работающий producer этих событий, отсутствуют проверенные рабочие owner/project bindings и отдельный owner-approved host unit. Поэтому шаблон службы **не установлен**, runtime SQLite на Netcup **не создавали**, производство не трогали. Активация требует проектной конфигурации и отдельной owner-controlled подготовки/переноса/активации релиза через ручные GitHub workflows. Версия на VPS по последней read-only проверке **`0.3.11a0` / `0e04af007375d82daf8f7d3b36c71abb0c784e0b`** (rollback SHA), проверить заново перед любой выкладкой. Статус **7.7 [ ] PARTIAL / HOST DEPLOYMENT HOLD**; 7.8 не начинать.

**Проверки подготовки:** `python -m unittest tests.test_project_diagnostics tests.test_project_diagnostic_adapter -q` — 27 test cases, 1 skipped (AF_UNIX bind sandbox EPERM), PASS; полный `python -m unittest discover -s tests -q` — **812 OK (2 skipped)**; `compileall`, public-source и workflow boundary scanners — PASS. Wheel `ai_agent_engine-0.3.14a0-py3-none-any.whl` построен и содержит оба ядра + intake; SHA-256 `4be0aadd265ee169fbfb392f31bfd22c8e48207ab9dd5059c2f4aad0abcb8153`. Реальный AF_UNIX socket E2E и Netcup live QA **не** проведены. Ни Windows, ни Netcup не меняли. Предыдущий GitHub HEAD для source rollback: `573c72b1202745f7d852c68a3ec9d3de384f7ec6`.

## Последний запрос владельца — начать подключение 7.7 к Netcup

Владелец после предыдущего ограничения ответил «Давай» на предложение перейти к подключению 7.7 на Netcup. Это разрешает **проверку и подготовку подключения 7.7**; Windows, новые расходы, секреты/права доступа и чужие данные по-прежнему вне задачи. Последующая live проверка **не** является выкладкой и не доказывает, что интеграция запущена.

**Новая read-only проверка сервера, 20.11.2026:** [GitHub Actions run 44698257792](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44698257792) — **PASS**, активны `ae-controller.service`, `ae-backup.timer`, `ae-host-check.timer`; `/opt/ai-agent-engine/current` → `releases/0e04af007375d82daf8f7d3b36c71abb0c784e0b` (установленный пакет `0.3.11a0`, зафиксированный rollback SHA). Запрос выпущен отдельным GitHub commit `f818f903d838391b9701f1662d93e5bc1a9016f2`; сам Netcup не менялся.

**Проверенные блокеры до любой активации:** в **точном источнике работающего релиза** нет `project_leader.py` (7.5), `project_diagnostics.py` (7.7) и `project_diagnostic_adapter.py`; `controller.py` — heartbeat/systemd, **не task execution/event loop**. Поэтому простая замена wheel `0.3.11a0` на текущий `0.3.13a0` не подключит диагностику к задачам. Нужно сначала реализовать и утвердить конкретный серверный task runner с проверенным owner/project binding, источниками FAILED-событий, отдельным приватным проектным ledger и восстановлением/QA; это новая runtime-инфраструктура и изменение жизненного цикла, а не существующий процесс, к которому можно подключиться. Кроме того, GitHub release preparation/transfer/activate требуют **ручного owner `workflow_dispatch`** и exact SHA/wheel/rollback для каждой стадии — не обходить.

**Действие после preflight:** live deployment **HOLD**, никаких изменений Windows, Netcup, systemd, SQLite/рабочих данных, SSH/правах доступа, подписках и секретах. Код `0.3.13a0` и 7.7 bridge уже опубликованы в GitHub (commits `c76d8ea` и `4732946`), offline проверки `807 OK (1 skipped)`, Linux CI и wheel PASS; Windows CI FAIL по старой ошибке и вне scope. **Перед продолжением:** согласовать отдельно запуск отсутствующего серверного task runner и допустимую схему owner/project-runtime данных, затем разработать и проверить полный release candidate на базе live source; прямо перед owner-controlled deploy сверить current SHA и подготовить rollback. Статус **7.7 [ ] PARTIAL / NETCUP RUNTIME BLOCKED**, не начинать 7.8 автоматически.

## Последнее указание владельца — действующие границы

**20.11.2026 владелец вновь выбрал вариант А подключения ядра 7.7 и прямо распорядился: Windows не изменять, Netcup не изменять без нового отдельного разрешения.** Для **этого задания** прежнее разрешение на Netcup из истории ниже не использовать. Серверный deployment, новые systemd процессы, новая runtime SQLite и live-вызовы запрещены до следующего явного согласия. Разрешённый результат сейчас — GitHub-исходники, документация и offline проверки. Нет оснований считать 7.7 полностью подключённой на сервере: **[ ] PARTIAL / LIVE HOLD**.

**Проверенная публикация до текущего уточнения:** GitHub `main` `c76d8ea48008ad673481ced2595ba2cbad5df0d3`, пакет исходников `0.3.13a0`: offline ядро и доверенный opt-in `TaskDiagnosticAdapter`, 807 unittest OK (1 skipped), Linux CI Python 3.11/3.12/3.13 и wheel PASS; Windows CI всё ещё FAIL из-за `fcntl` и отдельной существующей проверки, Windows не править. Нет изменений Netcup; известный по прежней read-only проверке релиз сервера `0e04af007375d82daf8f7d3b36c71abb0c784e0b` (не новый замер). Если требуется live завершение, сначала новое согласие и отдельная проверка текущего deployed SHA. Для отката GitHub текущего протокола использовать адресный `git revert` после сверки HEAD, без force-push.

## Текущее состояние — отдельное разрешение на Netcup получено

Владелец 20.11.2026 **разрешил адресные изменения существующего Netcup для подключения 7.7**. Прежние указания ниже «нет разрешения на Netcup» относятся к состоянию **до** нового согласия. Windows, новые расходы, секреты и права доступа, рабочие данные без дополнительного согласования остаются вне области изменений.

**Факт live-статуса 20.11.2026:** штатный GitOps read-only workflow [44696209219](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44696209219) **PASS**: `ae-controller.service`, `ae-backup.timer`, `ae-host-check.timer` активны; `/opt/ai-agent-engine/current` → `releases/0e04af007375d82daf8f7d3b36c71abb0c784e0b`. Это зафиксированный **исходный серверный SHA для отката**, его необходимо повторно проверить непосредственно перед выкладкой. Источник этого SHA взят из GitHub для проверки; действующий `controller.py` обслуживает heartbeat/systemd и **не** запускает TaskInbox или GPT/Claude. Коммит `2210d0329f78f3ac9b35f1c575fbbeb0d4000f82` добавил исключительно GitOps status-request — без изменения Netcup.

**Подготовленная интеграция, исходники `0.3.13a0`:** `ai_agent_engine/project_diagnostic_adapter.py` — вызываемый только доверенным runtime bridge между durable `TaskInbox` и `ProjectDiagnosticsStore`: требует подтверждённый `FAILED` transition именно назначенного worker, реальный Git baseline назначенного workspace, совпадающий owner/project source-binding и выводит idempotency key из ID события, а не текста модели. Добавлены offline тесты `tests/test_project_diagnostic_adapter.py`. Нет автозапуска, моделей, новых grants, API, правок runtime SQLite и автоматического восстановления действий. **Мост ещё не подключён к выполняемому серверному task runner**: его нет в развёрнутом контроллере; серверный пакет **не заменён**, 7.7 **[ ] PARTIAL / LIVE NOT DEPLOYED**.

**Остаётся/блокер:** определить утверждённый host task runtime и отдельную допустимость его запуска и новой диагностической runtime SQLite (рабочие данные); подготовить host acceptance и rollback. Существующие owner-only GitHub release transfer/activation требуют **ручного `workflow_dispatch` от `vl-ktk`** и подписанного release intent — не обходить. Перед deployment сверить current SHA, взять исходник выложенной версии, проверить candidate и wheel/manifest, затем разрешённый запуск и read-only host приёмку. Windows и Netcup-сервисы не изменялись. GitHub rollback до кода адаптера: `main` `2210d0329f78f3ac9b35f1c575fbbeb0d4000f82`; после публикации использовать адресный revert feature-коммита без force-push.

**Реальные offline-проверки:** новый bridge + core: `22 OK`; полный `unittest discover`: `807 OK (1 skipped)`; `compileall`, `check_public_source.py`, `check_workflow_boundaries.py` — PASS. `pip wheel --no-deps --no-build-isolation` собрал `ai_agent_engine-0.3.13a0-py3-none-any.whl` (`sha256 0011302c173cfc7e8b4ef3ce8d40c35816866c121b3f842dcdfb9fabada11c48`), архив содержит оба 7.7-модуля. В исходной среде отсутствует `python -m build`, поэтому сборка выполнена доступным `pip wheel`. Никаких серверных интеграционных тестов GPT/Claude и live-приёмки не было.

## Текущая задача 7.7

**Новое решение владельца 20.11.2026 — вариант А прямого подключения диагностического ядра к работающему оркестратору Netcup.** Выбор режима одобрен, но **не дано отдельного разрешения изменять Netcup**: владелец одновременно потребовал не менять Netcup без такого разрешения и не менять Windows. Статус **7.7 [ ] PARTIAL / LIVE BLOCKED**; не считать выбор А выполненной выкладкой, не запускать VPS, внешние модели, миграции, диагностические сборщики, платные сервисы или live-тесты. Что осталось: явное разрешение на изменения Netcup; read-only сверка работающей версии и rollback перед работой, доверенный task/grant/source/QA adapter, безопасные интеграционные тесты, повторная проверка версии непосредственно перед разрешённой выкладкой и live-приёмка. Отдельные ресурсоёмкие функции требуют будущих замеров/разрешений. 7.8 не начинать автоматически.

**Публикация решения А:** только изменение архитектурного протокола, roadmap и этого HANDOFF в GitHub. Исходный `main` до записи — `fcfe3ea4757ba6095aa2948fd7dd9eb8699abf91` для точечного `git revert` после проверки HEAD. Код/версия пакета `0.3.12a0` и реальные системы не меняются. Предыдущее решение вопроса №10/В об offline ядре сохраняется.

**7.7 [ ] PARTIAL — архитектура №1–10/В утверждена Владиславом 20.11.2026; проверяемое Linux/offline ядро реализовано.** Реализованы `ai_agent_engine/project_diagnostics.py`, `tests/test_project_diagnostics.py` и пакет `0.3.12a0`: в доверенном `ProjectLeaderStore` только одобренные owner/project; отдельная private SQLite `diagnostics_7_7.sqlite3` (не центральная task DB), жёсткая binding-карта Git repo ↔ project, повторно проверенный Git baseline/result/dirty SHA 6.13, bounded sanitized failure/immutable occurrence/группировка fingerprint, history/audit, idempotency, максимум три зарезервированные до эффекта попытки, QA callbacks с recheck и требованием фактического изменения версии, `HOLD` после неопределённого crash/recovery без replay. Подробные логи/секреты/неавторизованные команды не выполнять и не собирать. **Проверки: 17 новых offline тестов PASS, полный unittest: 802 OK (1 skipped), compileall PASS, source/CI boundary scanners PASS, wheel `0.3.12a0` собран и содержит модуль.** Реальная семантическая оценка гипотез, независимая идентичность QA-исполнителя, живые task/grant/process gates, приём из GPT/Claude, архивные бюджеты/TTL, очистка и серверная приёмка ещё **не реализованы/не разрешены**; не объявлять 7.7 полностью закрытым, 7.8 не начинать без отдельной команды. [Протокол](./infra/orchestrator/PROJECT_DIAGNOSTICS_7_7.md).

**Публикация offline ядра 7.7 №10:** код, тесты, дорожная карта и протокол в GitHub `main`; предыдущий SHA для адресного отката до публикации `a9406880cdb4807ee39ab1016cd264f140077629` (только `git revert` после проверки HEAD; без force-push). Новая версия Python-пакета `0.3.12a0`, исключительно исходники без deployment. **Windows и Netcup не менять:** ни запусков, ни live-проверок, ни расходов, ни правок workflows. Следующий шаг — отдельное согласование живой интеграции и замеров после приёмки offline ядра.

**CI GitHub после публикации [f8f33fe](https://github.com/vl-ktk/ai-agent-engine/commit/f8f33fe0a8b3a166c11ce3c894a22e89cb705bd6):** [run 44693742817](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44693742817) — Ubuntu Python 3.11/3.12/3.13 **PASS**, Build and exercise isolated wheel **PASS**; Windows Python 3.12 **FAIL** (в логе `ModuleNotFoundError: fcntl`, всего 30 import errors и 1 failure), поэтому общий CI **не зелёный**. Windows не исправлять и workflow не менять без отдельного указания владельца. Для отката реализации требуется проверка HEAD и адресный revert feature-коммита `f8f33fe0a8b3a166c11ce3c894a22e89cb705bd6`; предыдущее состояние исходников `a9406880cdb4807ee39ab1016cd264f140077629`.

## Предыдущий выполненный этап 7.6

## Текущая задача

**7.6 [x] в согласованной offline-границе №10 / В.** Все архитектурные вопросы №1–10: выбор **В**; детали — `infra/orchestrator/PROJECT_LEADER_7_6.md`. Реализован доверенный `ProjectMemoryStore` (`ai_agent_engine/project_memory.py`) поверх approved 7.5 project workspace: проектная SQLite v2 с атомарной миграцией v1, значимые append-only события с provenance и проверяемым статусом, idempotency, supersession, краткая сводка с CAS `expected_revision`/историей/безопасным восстановлением, bounded scoped SQLite search и выдача маркированного недоверенного JSON-контекста при обязательном настоящем счётчике токенов. Явные секреты отбрасываются при записи, некоторые явные prompt injections дают audit/HOLD. Не добавлялись полномочия owner, specialist/LLM, grants или доступ к чужой SQLite. **Границы безопасности:** доверенный вызывающий слой отвечает за настоящую проверку scope/внешнего источника; `verified` и SHA сами по себе не являются внешней аттестацией; эвристика injection/secret не исчерпывающая, свободный текст семантически не верифицирован. **OPEN / DEFERRED** без измерений/разрешений: FTS5/embeddings, semantic rerank, фоновые/пакетные индексаторы, owner-wide каталог уроков, живые модели, сетевой UI/auth, производственные квоты/TTL и интеграция backup. Только code/offline, не deploy.

**Windows и Netcup:** владелец прямо запретил их изменения. Workflow Windows не исправляли, прежняя Windows CI failure Linux-only `fcntl` остаётся; новые live-проверки не проводили. Нет изменений Netcup, рабочих данных, секретов, платных сервисов и прав доступа.

**Проверки:** `tests/test_project_memory.py` — **23 PASS**; тесты 7.5 — 19 PASS; весь проект — **785 unittest OK (1 skipped)**; `compileall`, source/workflow checks и wheel — результаты дополнительно ниже/в итоговом отчёте. Исходник для новой реализации: публичный GitHub `main` `05959feab4171b8a5cb6bb1198ced997a0dd057a` (точный id до изменений для отката). Выложенная source revision — новый commit `main` по проверке публикации; Python distribution остаётся `0.3.11a0`. Откат исходников: адресный `git revert` коммита 7.6 после перепроверки HEAD, **без force-push**. Netcup-откат не требуется. Следующий 7.7 запускать только по отдельной команде владельца.

## Предыдущий выполненный этап 7.5

**7.5 [x] в выбранной владельцем границе №11 / А, offline.** Вопросы №1–10: архитектура согласована (изолированные project-scoped папки/SQLite, однократное создание после owner approval, управленческие материалы без копии исходников, scoped права/гранты, безопасность параллельной работы, восстановление, резервные копии, слои хранения, lifecycle). Точные решения и оговорки: `infra/orchestrator/PROJECT_LEADER_7_5.md`. №11 / А разрешает сейчас только фундамент 7.5; согласование архитектуры остальных возможностей **не означает**, что они реализованы.

### Что реализовано в исходнике

- `ai_agent_engine/project_leader.py`: инертная scoped-заявка `draft_project` с SHA-256; точная проверка аутентифицированного вызывающим кодом `OwnerDecision`; реестр одобрений в собственной приватной `project-leaders.sqlite3`. Идемпотентное создание после одобрения, устойчивость к прерыванию состояния `provisioning` и fail-closed на исчезнувшей/чужой активной БД.
- Раздельные `<projects_root>/<owner_id>/<project_id>/leader.sqlite3` и `documents/`, каталоги 0700, файлы 0600, owner/project scope и проверка symlink, владельца/режима/идентичности. Общая task SQLite и очередь не изменены; постоянная копия кода не создаётся.
- Минимальный `management_refs` с типизированными ссылками на план/задачи/решения/QA/ошибки/checkpoints/историю/контекст/исходник. Только доверенный локальный store; `verified` фиксирует заявленную отметку и checksum, но **не удостоверяет** внешний источник самостоятельно.
- `tests/test_project_leader.py`: 19 offline-тестов на одобрения, повтор, параллельность, изоляцию, отказ при повреждениях/симлинках и восстановление provisioning. Совместимость с текущей task SQLite проверяется отдельно.

### Проведённые проверки

- `python -m unittest discover -s tests -p test_project_leader.py -q` — **19 PASS**.
- `python -m unittest discover -s tests -q` — **762 tests OK, 1 skipped** (полный offline-регресс после финальной реализации).
- `python -m compileall -q ai_agent_engine`, `python scripts/check_public_source.py`, `python scripts/check_workflow_boundaries.py` — **PASS**.
- `python -m pip wheel --no-build-isolation --no-deps .` — **PASS**, пакет `ai_agent_engine-0.3.11a0-py3-none-any.whl` (version пакета не изменяли, это только новая исходная ревизия 7.5). Живых E2E/Netcup проверок не было.

### Границы и следующее

**Не включено:** HTTP/UI/достоверная идентификация владельца в сетевом интерфейсе, живые модели, запись специалистов, сквозные runtime grants, полноценная память 7.6, CAS/merge документов, восстановление задач, backup/restore, автоочистка, PAUSED/ARCHIVED. Без новых owner-approved квот и полных интеграционных тестов эти функции не активировать. **Netcup не трогали** — изменения только в GitHub-исходнике; live/deploy проверки не выполнялись.

**Следующий плановый пункт:** 7.6 после отдельной команды владельца, начиная с чтения этого HANDOFF и проверки актуального `main`. Прежний HEAD до 7.5: `53d64eae7cafcd66a76eb13b681822afd8bc5824`; для отката исходников использовать адресный `git revert` нового коммита 7.5 после сверки актуальной ветки, не force-push. Netcup откат не требуется, потому что выкладки не было.

## Предыдущий выполненный этап 7.4

## Задача и решение владельца

Пункт **7.4 [x] в согласованной offline-границе №9 / А**: owner-only переназначение специалистов после полной проверки старого process-group, новой специализации, grants, checkpoint и точного SHA/TTL согласия. Все архитектурные решения №1–8 записаны в `infra/orchestrator/MAIN_ORCHESTRATOR_7_4.md`; дорожная карта обновлена. Кризисный узкоспециализированный вызов из отдельного `CRISIS_SPECIALIST_ESCALATION_POLICY.md` **не является** переназначением 7.4.

## Что сделано в GitHub-исходнике

- `ai_agent_engine/main_reassignment.py`: предложение и owner-карточка, согласование `OwnerDecision`, immutable identity `task_id`+`attempt_id`, чекпоинты SHA с verified/unverified и независимой QA-границей, новый attempt после FAILED/CANCELLED/TIMED_OUT только с `EffectClearance`, запрет на SUCCEEDED, scoped history, отказ при истечении/устаревании approval и повторная проверка перед `ready`.
- `ai_agent_engine/task_storage.py`: приватная схема SQLite v14 и миграции старых v1–v13, таблицы proposal, attempts, current, artifacts и событий. Изменение **только** приватного формата при открытии будущей версии; чужие БД не изменять.
- `ai_agent_engine/task_api.py`, `task_control.py`, `task_permissions.py`: блокировка повторного запуска/смены старого `TaskState` и старых действий после переназначения; per-task barrier предотвращает гонку между owner transfer и чувствительным callback.
- `observe_unavailable` принимает доверенную, утверждаемую отдельно политику приоритетов с конечными сроками/числом опросов и **только советует** `wait` или `suggest_alternative`; сам не меняет агента, не выдаёт права и не запускает модель. Предложение нового агента всегда требует нового owner approval.
- `tests/test_main_reassignment.py` плюс обновлённые ожидания private schema v14 в миграционных тестах. Fake-процессы, негативные grants/hold/scope/TTL/replay/checkpoint/priority и callback-race проверяются offline.

## Граница результата

**Ничего на Netcup не выкладывали и не меняли.** Реальные GPT/Codex/Claude Code, подписочная авторизация, рабочая панель с кнопками и production task-runner этим этапом **не активированы**. Метод `activate_transfer` записывает только `ready` (не процесс). Отсутствуют утверждённые числовые wait/queue-пороги и окончательная owner-approved policy версии 7.11–7.12; тестовые значения не являются рабочими настройками.

## Проверки и версионность

- Проверка модуля 7.4: **21 unittest PASS**; полный прогон после последних правок и wheel/compileall — см. журнал текущей реализации/коммит GitHub.
- Начальный исходник `main` для реализации: `34b856690694b6923e33fa2ab2cda328717d3775`; исходный tree `88d248dd667c1646231821218994ef67a5e85e4d`.
- Netcup **не проверялся и не менялся** в этой задаче. Последний ранее зафиксированный в проекте выпуск: `0.3.11a0`, release `0e04af007375d82daf8f7d3b36c71abb0c784e0b`; это не свежая диагностика сервера. Для source rollback использовать адресный `git revert` коммита 7.4 после проверки актуальной ветки, **без force-push**. Netcup rollback не нужен, потому что выкладки не было.

## Следующее

Перейти к обсуждению следующего архитектурного пункта по очереди дорожной карты. Перед реальным включением переназначения понадобится trusted owner-authenticated UI/adapter, отдельная execution integration нового `attempt_id`, проверка подписок/сессий, approval конкретных приоритетных числовых лимитов, затем отдельные live/E2E и решение владельца о production deployment. Не считать текущий offline PASS доказательством работы моделей на сервере.
