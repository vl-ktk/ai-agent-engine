# HANDOFF — AI Agent Engine

## Stage 4.1 — Netcup order submitted; provider review pending (2026-11-17)

- **Owner-approved purchase:** netcup **VPS Lite 2 G12.5s iv 3M**, x86 controller VM only (4 vCore, 8 GB RAM, 160 GB SSD, IPv4+IPv6, EU location automatically assigned on provisioning). Owner authorized up to **€30 total for the first 3 months** and explicitly confirmed the binding order after reviewing the exact price and auto-renewal.
- **Verified Ukrainian checkout:** client country Ukraine; **€7.98/month, billed every 3 months, €23.94 first invoice/3 months, 0% VAT, €0 setup**. Invoice breakdown: base server **€22.44/3 months** + IPv4 **€1.50/3 months** + IPv6 **€0**. No extra products. **Contract minimum 3 months; auto-renews by contract term unless cancelled at least 31 days before expiry**, per checkout notice.
- **Order evidence:** after the owner explicitly said «Давай», submitted **“Order at charge”** on the official netcup checkout and saw **“Thank you for your order!”** at https://www.netcup.com/en/checkout/thank-you. Provider's exact message: “Your order will now be checked by one of our employees. Once your details have been successfully checked, we will process your order.”
- **Current truth:** order **SUBMITTED / PENDING MANUAL REVIEW**. No Netcup approval, customer/control-panel credentials, payment confirmation, invoice ID, actual datacenter, server ID or running VPS observed. **Do not mark VM active or paid. No infrastructure, production, token or model changes.** Checkout form personal details must **never** be copied to GitHub.
- **Next actions:** watch account email for verification, acceptance, invoice and CCP/SCP credentials; owner handles any identity challenge and payment privately. After provider approval, verify invoice/amount (do not pay extra without approval), allocated EU region, VM active status, IP, and update Stage 4.1 evidence. Independent off-site backup remains **unfunded/unimplemented**. Stage 1.6–1.7 GPT/Claude unattended GO/NO-GO still **HOLD**; Stage 4.2 setup is **NOT authorized** yet.
- **Stage status:** 4.1 **[ ] pending allocation/activation and explicit GO decision**. Only the order submission has completed. Technical product comparisons and previous decisions are archived below and in [STAGE4_VPS_SELECTION.md](./STAGE4_VPS_SELECTION.md).
- **Git source/rollback baseline for this documentation update:** `main` `21bd877b155b51f033c8a8a737d4f3d787fcf406` before edits. Documentation-only branch; no code or deployed service changed. PR/CI details to be filled after validation.

## Stage 4.1 — owner-approved shortlist update (2026-11-17)

- **Decision:** owner accepted **OVHcloud VPS-2 primary, Hetzner CX33 backup**, only for controller VM. This supersedes the previous CX33-first recommendation; detailed comparison and official sources are in [STAGE4_VPS_SELECTION.md](./STAGE4_VPS_SELECTION.md).
- **No spending permission:** shortlist priority approval is **not** approval of pricing, monthly budget, rental, account provisioning or unattended ChatGPT/Claude execution. Stage 4.1 stays **[ ]**, Stage 4.2 untouched, Stage 1.6–1.7 VM GO/NO-GO still HOLD. No secrets, chargeable API, VM, or live sessions touched.
- **Public configurator live observation (2026-11-17):** country selector Ukraine maps to IE English / EUR storefront; VPS-2 No commitment €8.49/month excl. taxes for public configurator's Canada-East selection; **Europe displayed “No location available at the moment.”** No EU quote, account-specific Ukrainian taxes, stock/order or billing verified. Official evidence and source link in STAGE4_VPS_SELECTION.md. Do not automatically use Canada or a more expensive VPS. [PR #22](https://github.com/vl-ktk/ai-agent-engine/pull/22) MERGED as `d26b31b130543f6f4cb3bfd91b10853f3811bb73`; [PR CI 44073427782](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44073427782) **4/4 PASS** Linux 3.11–3.13 and Windows 3.12. Prior `main` rollback `ee002051bd23febd999ab26882baaa1bbc738d0b`. Docs-only, no production changes.
- **Outstanding:** OVHcloud VPS-2 price for Ukraine, actual stock in chosen EU region, month-to-month/advance commitment, included backup vs 7-day premium, taxes, renewal. Next request a distinct budget/purchase authorization before order.
- **Source/rollback:** [PR #21](https://github.com/vl-ktk/ai-agent-engine/pull/21) MERGED as `e78222f4d3efb9ba432352377c37ce3e537d381f`; branch head `0a6ecb771e320d5a903e971cae8ef68616df312e`. [PR CI run 44072264674](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44072264674): **4/4 PASS** (Ubuntu Python 3.11, 3.12, 3.13 + Windows Python 3.12; offline tests, build, demo, scan). **Rollback to prior main** `dbf38514b9e5342ecf824c44be400756552b4057`. Documentation-only change, no production deployment.

## Stage 4.1 — controller VPS selection (owner decision pending; 2026-11-17)

- **Source/rollback baseline:** `vl-ktk/ai-agent-engine` `main` `1c372b75c30bcd1cd1b51addae7d0c88997b4dc4` before this documentation. No runtime or production changes.
- **Owner decision:** 3.7 and global Stage 3 **accepted** 2026-11-17. Roadmap 3.7 and global 03 marked `[x]`, with the explicit boundary: the experimental offline engine is accepted, not autonomous production. Owner requested moving only to original 4.1.
- **4.1 research:** [STAGE4_VPS_SELECTION.md](./STAGE4_VPS_SELECTION.md) records official 2026 Hetzner prices and a *candidate*, not an order: x86 CX33, 4 shared vCPU, 8 GiB RAM, 80 GB NVMe, €8.49/month excluding VAT, backups 20%, IPv4 €0.50 -> ~€10.69 before taxes. Stock and checkout price unverified; fallback requires owner decision. This must not be confused with the separate 125B/GPU/iPad plans.
- **Blockers:** 1.6–1.7 VM/Claude unattended GO/NO-GO still HOLD; explicit VPS/monthly budget approval and exact checkout price are missing. **4.1 remains `[ ]`; no server, payment, token transfer, live model or other project action. Do not start 4.2 yet.**
- **Next owner action:** decide on candidate CX33 and budget ceiling (proposed ≤€15/month including taxes for VPS/backups/IPv4); once approved and stage-1 gates resolved, recheck availability/price before any provisioning. [PR #20](https://github.com/vl-ktk/ai-agent-engine/pull/20) MERGED as `0bc82d8fd62c0f806751d1c1ee17864f9afd0a14` (branch commit `7449bd1dc54f827f92a72192b78f4dd92172e44c`). [PR CI run 44070664332](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44070664332) **4/4 PASS** Linux Python 3.11–3.13 and Windows Python 3.12. **Rollback to pre-4.1 `main`: `1c372b75c30bcd1cd1b51addae7d0c88997b4dc4`.**

## Stage 3.7 — audit and owner acceptance gate (2026-11-17)

- **Source/rollback:** `vl-ktk/ai-agent-engine` `main` at `f0483fc18813884b907859614c6400cfd606a8fb` immediately before 3.7. Only this project; no production, other repositories or provider calls.
- **Owner decision:** stage 3.6 accepted on 2026-11-17 after PR #18, PR CI 4/4 and final main CI 4/4; roadmap 3.6 marked `[x]`. Owner commissioned **3.7 only**.
- **Audit:** see [STAGE3_ENGINE_AUDIT.md](./STAGE3_ENGINE_AUDIT.md), including explicit GO for synthetic offline prototype and NO-GO for autonomous production. Proven defects: final model answers could be empty/non-string while returning `done`, prompts were unbounded, and source scanner accepted NUL-containing files. Fixed in `core.py` and `scripts/check_public_source.py`; added integrated security regression `tests/test_engine_audit.py`, expanded scanner test; preview package version `0.3.7a0`.
- **Checks:** baseline 168/168; 171/171 local synthetic tests PASS; compileall and source scanner PASS. Built preview wheel `0.3.7a0`, installed without network in a fresh venv, and ran offline demo outside checkout PASS. [PR #19](https://github.com/vl-ktk/ai-agent-engine/pull/19) MERGED as `ddce1ee11488d7cbe90ac7806acc08db38eeb1cb` (branch audit commit `26e7504113591cd03f16120e30835648e93161ad`). [PR CI run 44069601297](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44069601297): **4/4 PASS** on Linux Python 3.11–3.13 and Windows Python 3.12, including install and smoke. **Rollback to pre-3.7 `main`: `f0483fc18813884b907859614c6400cfd606a8fb`.** No real credentials, paid API, VM or deployed service used.
- **Acceptance:** owner accepted Stage 3.7 and commissioned preparatory Stage 4.1 on 2026-11-17. Roadmap 3.7 and global 03 `[x]`; later VM, orchestration, OS isolation, durable state, authorization and provider policy/quotas remain pending. This is not production authorization.

## Stage 3.6 — package install, CI and safe public source (2026-11-17)

- **Source/rollback baseline:** `vl-ktk/ai-agent-engine`, `main` at `1087d02986c2c22f4bfce478cba372f0ce9785aa` before this stage; all changes confined to AI Agent Engine.
- **Owner decision:** 3.5 accepted on 2026-11-17 after PR #17 and CI 4/4 PASS, now marked `[x]` in the single roadmap. Owner authorized **only 3.6**. Do not start 3.7 without a new decision.
- **Implementation:** package metadata pinned to `setuptools==84.2.1`, version `0.3.6a0`, zero required runtime dependencies and optional `[siwc]`; Linux/Windows CI adds tracked-file secret scan and installed-package smoke check from an external directory. `ModelRouter` handles provider adapter initialization errors with a fixed non-sensitive message. Auth status catches malformed/non-finite expiry without a traceback. New synthetic regression tests.
- **Local validation:** baseline 161/161; new 168/168 offline unit tests PASS, compileall PASS, offline demo PASS, Git index scan PASS, `git diff --check` PASS. Built an offline wheel, installed in a fresh venv and ran demo outside checkout PASS. [PR #18](https://github.com/vl-ktk/ai-agent-engine/pull/18) MERGED into `main` as `c388d97e4ab1cd7ad5c158d6f2060dba6a631712` (branch code commit `8a78d9d44b3e2530c5c6da9777631b5bb99d103f`). [PR CI run 44068027837](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44068027837): **4/4 PASS** on Linux Python 3.11–3.13 and Windows Python 3.12, including install from source and smoke outside checkout. **Rollback to immediately previous `main`: `1087d02986c2c22f4bfce478cba372f0ce9785aa`.**
- **Known limitations:** scanner rejects only known high-confidence secret patterns and forbidden file categories, not every possible credential; tests are synthetic. Optional SIWC dependency versions are ranged, not cryptographically locked. No live providers, personal secrets, VM, deployment, paid usage or other repositories touched. **Owner accepted 3.6 on 2026-11-17; now `[x]`.**

## Stage 3.5 — multi-provider model routing (2026-11-17)

- **Source/baseline:** `vl-ktk/ai-agent-engine`, `main` revision `5a0fb5c36095f554548a0d63b26745484c88255d` before 3.5. This is the rollback baseline; work only on a branch based on this head.
- **Owner decision:** 3.4 accepted on 2026-11-17 and marked `[x]`. Owner authorized **only 3.5**; 3.6 and later remain untouched.
- **Changed:** new `model_routing.py` with explicit provider/model selection, trusted model factory registration and tool-capability preflight; generic `TextOnlyModel` for safe one-turn text callbacks; public exports, README and 3.5 offline regressions. The existing GPT `OpenAIPlanModel` was checked through fake transport; no actual Claude task adapter or provider request was implemented.
- **Local tests:** baseline 148/148; after 3.5 161/161 offline PASS. Compiled/demo/diff check before publishing; PR/CI/merge details recorded below when completed.
- **Status:** [PR #17](https://github.com/vl-ktk/ai-agent-engine/pull/17) MERGED as `7da9048b7086a65a364d9189e82d96df49751461` (branch commit `6a92fbf937ce25ffd4391608eae8e535a8967150`). [GitHub Actions run 44065059155](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44065059155): 4/4 PASS on Linux Python 3.11–3.13 and Windows Python 3.12. **Owner accepted 3.5 on 2026-11-17; roadmap 3.5 now `[x]`, authorized 3.6 only.**
- **Limitations:** per-provider usage quota, unattended Claude Pro authorization, production OAuth sessions, API billing, real tool translation, process isolation and VM deployment are deliberately out of scope; see stages 1.6–1.7 and 6.

## Stage 3.4 — tool registry, validation, sandbox and audit (2026-11-17)

- **Source/rollback:** `vl-ktk/ai-agent-engine` `main`, initial HEAD `45c2c33c124a7250151bf3204d83675315511a8e`. Working source for 3.4: only that version; rollback by reverting 3.4 PR or returning to the initial HEAD.
- **Owner decision:** 3.3 accepted on 2026-11-17, marked `[x]` in the single roadmap; development commissioned **only for 3.4**, no changes to 3.5 or later.
- **Changed:** `core.py` adds `ToolRegistry` with explicit grants, strict `Tool` schemas, arg/result bounds and per-step content-free actor/time audit; `tools.py` hardens path handling, limited reads and POSIX no-follow directory-descriptor opens; `plan_agent.py` retains original GPT test schemas. New offline security tests, documentation and roadmap updated.
- **Checks:** before 3.4: 134/134 offline PASS. After 3.4: 148/148 offline PASS; `compileall`, demo and `git diff --check` PASS on local Linux. Live models, cloud runtime, production and other projects untouched.
- **Status:** [PR #16](https://github.com/vl-ktk/ai-agent-engine/pull/16) MERGED as `957a66d1027c2f3c845de226a2a78a1a86a6acc1` (branch commit `593ae6134dc74ff8a23fdcc15ab72d6550c15c1a`). [GitHub Actions run 44063455579](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44063455579): 4/4 PASS on Linux Python 3.11–3.13 and Windows Python 3.12. Final main CI [44063665571](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44063665571) 4/4 PASS. **Owner accepted 3.4 and authorized 3.5 on 2026-11-17; roadmap 3.4 now `[x]`.**
- **Known boundary:** Windows sandbox cannot claim race resistance against a malicious same-OS-user process; no OS process isolation or persisted audit. These are later infrastructure/system-orchestrator requirements.

## Stage 3.3 — unified task sessions (PR #15 MERGED; 2026-11-17)

- **Source/rollback baseline:** `vl-ktk/ai-agent-engine` `main` at `6a6152d6b904f4371e4a68efff60a80828e16be6` before phase 3.3. Working branch `feat/phase-3-3-unified-runtime-20261117`.
- **Changed:** `core.py` adds role-scoped single-task sessions, trusted session context and a cooperative stop token; `plan_agent.py` runs the existing GPT owner sandbox test through the shared session interface while keeping the native tool allowlist unchanged. `__init__.py` exports the new types. New offline role/cancel tests added; README and roadmap clarify limitations.
- **Status:** [PR #15](https://github.com/vl-ktk/ai-agent-engine/pull/15) MERGED as `503b12302055a532ca9b1b581f8be1797febdd7d`; rollback to immediately previous `main` `6a6152d6b904f4371e4a68efff60a80828e16be6`. Baseline 125/125; 134/134 local offline tests PASS. [GitHub Actions run 44061912134](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44061912134) 4/4 PASS (Linux Python 3.11–3.13, Windows Python 3.12), compilation, tests and demo PASS. **Owner accepted 3.3 on 2026-11-17 and approved starting 3.4.** No new model calls or production activity.
- **Remaining:** real GPT/Claude live behavior is a separate owner-approved acceptance, not implied by the offline PASS. Verified pre-merge `main` matched `6a6152d6b904f4371e4a68efff60a80828e16be6`; PR had no conflicts.


**Updated:** 2026-11-17 (PR #14 architecture reconciliation merged; phase 1 and technical Stage 2 gates remain open)
**Source of truth:** https://github.com/vl-ktk/ai-agent-engine, branch `main`.
**Latest docs merge:** [PR #13](https://github.com/vl-ktk/ai-agent-engine/pull/13) merged as `e1f765e38cabebfd8c155bc5e57172aaa603bfcb` (2026-11-17). **Rollback for PR #13:** `aa8b6f343a17038d05ba9e1de9d9900bf4dea9e9` (previous `main`). Offline CI [run 44041544686](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44041544686) 4/4 PASS; local 125/125 tests, compile, demo and diff check PASS. No runtime code was changed in PR #13.
**Current merged runtime revision:** `ea8989001111a045e026adf9063a27c733d03d67` (PR #12, owner-initiated GPT refresh preflight; CI 4/4 PASS). **Rollback before PR #12:** `0c43a3e63da0f5ae6d338ea3fb8fff254d67210d` (PR #11, docs only). Earlier PR #10 staged GPT+Claude owner-authorized LIVE 12/12 PASS; separate live refresh/revocation, quota and VM checks remain pending.
**Earlier proven GPT response code:** `3e78ed4afbfca3e80bd763946f07f3b088ac2800` — owner live `gpt-6.1-sol` PASS 2026-11-16.
**Rollback before cross-platform lock PR #6:** `a9f69c397a541497a92e517a29126d549631c905` (main HEAD immediately before merge).
**Rollback before combined acceptance PR #5:** `7e425a6e9b6da0e92de8b9566210583e82e2e619` (main HEAD immediately before merge).
**Rollback before GPT error-handling PR #4:** `93fca68992aac8b5d7e371a90864557f677b0052` (main HEAD before merge).
**Rollback before sign-out PR #3:** `9aa574dbdc18f0454386c2011c2a077dcc72e15d` (source main immediately before merge).
**Rollback to code before native GPT tool test:** `daaea985ddeab093b7acf7f9704eff5ee317a50c`.
**Rollback baseline before earlier inference merge:** `b942411c5d140a88df01f3393ae39969ec313b05`.
**Rollback baseline (before OAuth browser fix):** `715f651f0a9ca1403bdc6da320e3767e95a8260b`.
**Scope:** Only AI Agent Engine. Not KTK Social, ATHL, or the separate local-AI project.

## Stage 2 — architecture reconciliation (2026-11-17; documentation branch)

- **Source and rollback baseline:** `vl-ktk/ai-agent-engine` `main` at `6d0a8781cd0c1a23e64286d2517ceedc5d9a1957` before this document change. Working branch: `docs/stage2-reconciliation-20261117`. No runtime code or production deployment changed.
- **What is documented:** `STAGE2_ARCHITECTURE_SPEC.md` maps discussion topics 2.1–2.9 to the original 2.1–2.7 roadmap and records approved global leaders/project teams, scoped AI task reassignment, standing project-scoped release grants, safe rollback, cross-project sanitized knowledge, budget policy, critical alerts, reports, and individual future city avatars. `ARCHITECTURE_DECISIONS.md` and `IMPLEMENTATION_ROADMAP.md` are aligned.
- **Status:** conceptual 2.1/2.4/2.5 decisions accepted; exact engineering details 2.2/2.3/2.6/2.7 proposed, **not owner-approved**, so Stage 2 stays OPEN. Stage 3.1/3.2 remain completed; Stage 3.3–3.7 remain OPEN. Global Stage 1 and real refresh/revoke, VM, Claude unattended policy remain pending.
- **Next owner handoff:** select unresolved original Stage 2 technical contracts (source manifests and concurrency; database fields and state transitions; CPU controller vs optional GPU; exchange/acceptance/rollback). Only then mark corresponding Stage 2 lines complete. Independent Stage 3 offline work may be commissioned separately. No paid environment, credentials, external projects, model calls or deployment were changed in this documentation task.
- **Merged record:** [PR #14](https://github.com/vl-ktk/ai-agent-engine/pull/14) merged to `main` as `dd33341b6babb9202a75f6c50ce42b096ebd5d42`; immediate pre-merge `main` / rollback baseline: `6d0a8781cd0c1a23e64286d2517ceedc5d9a1957`. Code unchanged. [GitHub Actions run 44059970409](https://github.com/vl-ktk/ai-agent-engine/actions/runs/44059970409): **4/4 PASS** (Linux 3.11/3.12/3.13 and native Windows 3.12; offline tests, compile, demo). Verified seven original Stage 2 checklist items, eleven global stages and spec link. No live model or deployment run.


## Phase 1.8 — итоговый GO/NO-GO (2026-11-17; PR #13 MERGED)

- Источник: [PHASE1_GO_NOGO.md](./PHASE1_GO_NOGO.md); первичные документы OpenAI, Anthropic и Hetzner сверены 2026-11-17. Зафиксированы именно подтверждённые LIVE 12/12, реальный GPT OAuth, PR #12 refresh с офлайн CI 4/4 и границы того, чего эти проверки не доказывают.
- **GO** для этапа 2: проектирование архитектуры и дальнейшая офлайн-разработка без дополнительных сервисов и затрат. Для GPT и Claude Code есть только ограниченный личный пилот.
- **HOLD** для VPS/автономного исполнения до согласования расходов, живого refresh/VM и проверки безопасности; **HOLD** для постоянно работающего Claude Pro до ясности правил Anthropic. **NO-GO** для сторонних пользователей на единой личной подписке и для списаний без отдельного разрешения.
- Для сравнения бюджета: Hetzner CX33 — €8,49/мес. без НДС и IPv4, Primary IPv4 — €0,50/мес. по текущей официальной документации; сам CX33 на странице продукта показан временно недоступным. Никакой сервер не заказан.
- Пункт 1.8 подготовлен к приёмке владельца, но не помечается закрытым без неё по правилу дорожной карты. Глобальный этап 1, 1.6 и 1.7 сохраняют открытые вопросы. **Новых LIVE/платных действий, изменений токенов или production не было**.
- Следующее допустимое действие после приёмки: этап 2, подпункт 2.1 (границы компонентов); сам аудит не разрешает деплой или покупку VM.

## Phase 1.7 — automatic refresh on owner-initiated GPT requests ([PR #12](https://github.com/vl-ktk/ai-agent-engine/pull/12); MERGED)

- Branch `fix/phase-1-7-auto-refresh-inference` merged into `main` as `ea8989001111a045e026adf9063a27c733d03d67` (rollback `0c43a3e`); PR #12. No new machine, credentials, OAuth session, network model requests or paid resource used.
- Found a concrete inconsistency: Tasks 4/5 already refreshed near expiry, while `inference models/probe/ask` checked the saved bearer and could instead tell the owner to sign in again. `auth status` incorrectly promised the next model request would attempt refresh.
- Changes: shared `_fresh_access_token` preflights the existing cross-process-safe `refresh_if_needed` for model catalog, response text and every native-tool model turn; redacts failure through the existing `InferenceError` path and refuses model calls after refresh rejection. Both refresh and model transport remain injectable for offline tests. No new background polling or retries.
- Synthetic tests cover refresh before catalog, one rotation for multi-request text probe, a mid-cycle native-tool token rotation, fail-closed HTTP 401 preserving local session and an unreadable credential file. Initial local regression baseline: 120/120 PASS; amended local suite: 125/125 PASS. **GitHub CI final run 44040107261: 4/4 PASS** on Linux Python 3.11–3.13 and native Windows Python 3.12. **A real refresh remains unverified.**
- Next: one separate owner-authorized live credential refresh or lifecycle check when practical. Keep 1.7 OPEN; defer actual VM transfer to the approved VM phase.

## Phase 1.6(в) — official Claude Code integration review (2026-11-17; documentation-only)

- [Review: Claude integration boundaries](./CLAUDE_INTEGRATION_REVIEW.md): personal official CLI/IDE pilot — GO, unattended Claude Pro as future hosted orchestration backend — HOLD pending Anthropic policy confirmation and owner decision; routing other users via subscription OAuth — NO-GO. OpenAI SIWC VM is a separate integration.
- The 12/12 LIVE result verifies one short text response and GPT sandbox tools, **not** provider permission for continuous automation, quotas, paid Extra Usage configuration, expiry recovery or remote VM reliability. No additional provider request, credentials, production or cost was used in this review.
- Keep 1.6 and global stage 1 OPEN; subsequent 1.7 live credential lifecycle and 1.8 GO/NO-GO still need decisions. No unilateral change to the approved architecture.

## Phase 1.6 — staged combined acceptance, LIVE 12/12 PASS (PR #10; MERGED)

- Branch `fix/staged-live-acceptance`, [PR #10](https://github.com/vl-ktk/ai-agent-engine/pull/10). Live run executed code `bf2adf9`; follow-up `d6b2da7` only changes report-file error handling. **Merged to main as `282055c` on 2026-11-17; rollback = pre-merge main `03bb0c3`.**
- **Root cause of the owner's `GPT sandbox tools were not verified`:** established only that `model.last_error` was `None` (message had no suffix) → no HTTP/quota/stream/unauthorized-tool/argument error; sign-in, refresh and model list succeeded. The exact failing branch inside the agent loop/file check is **unknowable** because old `run_live` discarded agent status and audit. Unconfirmed candidates: no tool call, trailing period from the ambiguous prompt, tool rejection (e.g. `./path`), or a non-`InferenceError` exception swallowed by `Agent`. All are now reported per stage.
- **Confirmed defect fixed:** Claude probe used `--bare`; official CLI help states `--bare` never reads OAuth and only accepts `ANTHROPIC_API_KEY`, so it could not work on Claude Pro.
- New `acceptance run-live`: 12 stages PASS/FAIL/SKIPPED with redacted reasons; GPT and Claude independent; ≤1 GPT text + ≤4 tool-cycle + 1 Claude request; no retries; exit 0 only when all pass; `--report-json PATH`. Claude requires `authMethod=claude.ai`, first-party, `subscriptionType` pro/max; inherited `CLAUDE_CODE_*` env stripped.
- Offline: **120/120** tests (Python 3.12 Codespace). CI: see PR #10.
- **LIVE 2026-11-17 (one owner-authorised run in Codespace `laughing-disco-544rxwqgjxgcw74`):** all 12 stages PASS — `gpt-6.1-sol` (first of 8 visible) text reply; GPT called `write_new_file`, file content verified on disk; GPT called `read_file`, content verified; loop completed in 3 model requests. Claude Code 2.1.326: `claude.ai`, `subscriptionType=pro`, one `claude -p` reply completed with expected text. Only the optional JSON report failed to save (non-existent target dir); fixed + tested, not re-run live.
- Still open: Claude Extra Usage billing setting not verifiable via CLI; real quota exhaustion, refresh-after-expiry and revoke not exercised live; integration/legal review (1.6 (в)); 1.7 VM transfer. Keep **1.6 [ ]** until the integration review; stage 1 [ ].

## Phase 1.6 — one owner-only combined LIVE task (PR #9; OFFLINE PASS)

- Code: [PR #9](https://github.com/vl-ktk/ai-agent-engine/pull/9), main merge `4813d5ad5ece6f741e6c737de86c81e9d967fd06`; **rollback to immediately pre-merge main `4da6c9495be4b01a7a30dac2382cfb3eba7594d6`**.
- [CI 43974594112](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43974594112): **4/4 PASS, 102/102 offline tests** on Linux Python 3.11/3.12/3.13 and native Windows Python 3.12; compilation/demo PASS. The first PR run 43974475846 failed because an older test assumed exactly four tasks; amended that test and reran CI successfully.
- `.vscode/tasks.json`: owner-only task **5. Combined GPT + Claude acceptance (LIVE)** runs already implemented `python3 -m ai_agent_engine.acceptance run-live`. Its CLI preflights both local logins, then attempts the existing isolated GPT tool-cycle and one official Claude Code CLI reply. Old tasks 1–4 are preserved for debugging. Added tests against automatic execution and documented that a working VS Code mobile task picker is still a separate prerequisite.
- **No live tests were run**: not even `acceptance check` on the owner's Codespace. A real Claude Code installation/sign-in, current subscription quota, GPT tool-cycle, OAuth refresh/revoke and VM transfer remain UNVERIFIED. The owner need not touch Codespaces now.
- **Billing caveat**: Claude `auth status` reveals whether sign-in is `claude.ai` but cannot prove paid *Extra Usage* is disabled; owner should verify billing settings before any explicit live run. No charge was incurred in this PR. This test is not permission to route third-party services through Claude Pro; Anthropic's published terms restrict that.
- Keep **1.6 [ ]**, **1.7 [ ]**, global stage **1 [ ]** and all 11 roadmap stages unchanged. Later arrange ONE combined owner-assisted live check on a host with both personal logins, not repeated phone VS Code tasks.

## Phase 1.7 — native Windows credential lock verification (PR #8; OFFLINE PASS)

- Code: [PR #8](https://github.com/vl-ktk/ai-agent-engine/pull/8), merge `9431d8783c26e57da6733422b7dabf57c6c1f98a`; **rollback to pre-merge main `2c2c9a007a03ca9076a4dde3deda749f312c3b01`**. Source changes: `.github/workflows/ci.yml` and `tests/test_session_refresh.py`.
- [CI run 43973456667](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43973456667): **4/4 PASS** — Python 3.11/3.12/3.13 on Linux and Python 3.12 on **native Windows GitHub-hosted runner**. 101/101 offline tests per job; compilation and offline demo PASS. Both real OS processes competed for the same `.refresh.lock`; second process was correctly denied while the lock was held, and granted access after release. Separate child process also verified host ID persistence.
- This establishes Windows compatibility **in the GitHub runner environment only**. No actual owner's Windows 10 desktop, VM, persisted real OAuth tokens, live refresh/revocation, SSH transfer or paid resource was used. Repo is public, and GitHub documents standard hosted Windows runners as free for public repositories.
- Global stage **1** and substage **1.7** remain **OPEN**, awaiting grouped owner-approved live acceptance. Do not ask the owner to repeatedly operate Codespaces tasks on iPhone; continue only justified offline preparation. All 11 original global stages remain intact.

## Phase 1.7 — protected VM session staging (PR #7; OFFLINE PASS)

- Code merge: [PR #7](https://github.com/vl-ktk/ai-agent-engine/pull/7), merged main `326562bd02fa90354d8deed94bd082268cda2405`. **Rollback to immediately pre-merge main:** `25ad399c97600505f1ac506aebd176ea602f46a4`.
- [GitHub Actions run 43972363668](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43972363668): **3/3 PASS**, 99/99 offline tests on each Python 3.11, 3.12, 3.13; compilation and offline demo PASS. Earlier baseline: 84 tests each.
- Added `ai_agent_engine/vm_credentials.py`, `tests/test_vm_credentials.py` and README owner-only instructions. Setup produces one stable destination VM host ID, read-only dry-run checks protected incoming JSON, and separately gated `import-stdin --apply --confirm-vm-host` refuses overwrite, invalid source registration, host collision and nonmatching VM ID. Transport is only a caller-supplied protected stdin pipe; same OS lock as refresh/logout; destination host ID is preserved. Tests use **synthetic credentials**.
- **Not executed:** real import command; remote SSH or VM creation; accessing/moving owner's current Codespace `session.json`; GPT calls; OAuth refresh/revoke; native Windows tests; paid activity. The current saved owner sign-in remains untouched. No evidence that a real copied refresh token would still be accepted.
- Stage **1.7 remains OPEN**, and the 11-stage global roadmap is unchanged. Future actual import requires separate approval of phase 4 VM and one owner-controlled secure transfer. Until then, proceed with independent offline stage 1.6/1.7 refinements only, and later group live acceptance into one owner-assisted session. No further iPhone Tasks or passwords requested.

## Verified milestone — real ChatGPT OAuth successful

- **2026-11-16: phase 1.5 real-user sign-in PASS.** The owner used the remote Firefox desktop in existing GitHub Codespace `laughing-disco-544rxwqgjxgcw74`, approved their ChatGPT push/consent, and reached the local `127.0.0.1` OAuth callback. No home PC was needed.
- Real `python3 -m ai_agent_engine.auth login` terminal result: `Identity verified` for the owner's iCloud account; `ChatGPT plan permission: GRANTED`.
- Immediately repeated `python3 -m ai_agent_engine.auth status`: displayed saved identity, `GRANTED`, and an access-token expiry approximately one hour from issuance. The account identifier is intentionally omitted from this handoff to minimize personal data.
- Local secrets, access and refresh tokens were **not** printed, moved to GitHub or sent in the chat. They are in the Codespace user's protected app-data directory outside this public repository. Credentials are not replicated to the current agent runtime.
- Existing nonblocking browser fix: `ai_agent_engine/siwc.py` launches Firefox on a daemon thread while the loopback server waits for the callback. Before live authentication, 22/22 offline unit tests, compileall and offline demo passed. No production deployment.

## Phase 1.7 cross-platform session lock — offline PASS

**2026-11-17 PR #6** [Windows/POSIX session locking](https://github.com/vl-ktk/ai-agent-engine/pull/6)
merged to `main` as code version `04d2fdf93b377fb7fdd1f6db469709ee24bef52d`.
Rollback to prior `main`: `a9f69c397a541497a92e517a29126d549631c905`.
[GitHub Actions 43971352769](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43971352769):
**3/3 PASS** Python 3.11, 3.12 and 3.13 with **84/84 synthetic
offline tests on each version**, compile and demo PASS.

POSIX now uses bounded nonblocking `flock`; Windows uses bounded
single-byte `msvcrt.locking`; refresh and manual logout share a persistent
private lock file. Reject symlinks/nonregular file, limit wait, never leak
credentials to the lock file and do not rotate after lock acquisition fails.
**Windows was simulated in Linux CI**, not executed on a Windows host.
Existing Codespaces session, personal refresh/revocation, native Windows/macOS
and self-hosted VM import all remain **NOT tested**. No new VM, API cost or
production deployment. Keep phase 1.7 **OPEN**. Later owner-assisted
live acceptance is still necessary; do not request repeated phone tasks.

## Verified offline-only change — combined personal acceptance

**2026-11-17: PR #5** [combined owner acceptance](https://github.com/vl-ktk/ai-agent-engine/pull/5)
merged to `main` as code commit `087ea11ba7a49a863a9ab6f4233cc46a3fa6238a`.
Rollback to previous `main`: `7e425a6e9b6da0e92de8b9566210583e82e2e619`.
[CI run 43970399415](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43970399415)
is **3/3 PASS**, 78/78 synthetic offline unit tests each on Python
3.11, 3.12 and 3.13, compilation and offline demo PASS.

Source: `ai_agent_engine/acceptance.py`, `tests/test_acceptance.py`,
`README.md` and this handoff/roadmap. `check` uses the local saved GPT
grant and official `claude auth status`, accepts only `claude.ai` subscription
auth and rejects environment/API overrides. The explicit owner-only
`run-live` preflights both accounts before GPT's disposable native function
test and one official Claude `-p` reply; Claude runs with no tools/MCP,
plugins/hooks or persisted session. No retry or paid billing fallback.
**No real Claude Code login, model invocation, actual API-cost/source check,
GPT tool call, refresh, revocation or VM deployment was performed in PR #5.**
Do not run a phone VS Code Task now. Schedule one owner-operated combined
acceptance only when prerequisites are ready.

**Legal scope:** This is a personal evaluation harness, not authorization
to route other users' Claude subscription traffic through a product. Anthropic
states embedded/hosted agent infrastructure may require commercial terms
and product integrations should use API-key authentication; review before
any deployment or automation. Keep stage 1.6 **OPEN**.

## Current limitations and next step — 1.6

**New completion 2026-11-17: PR #4** [GPT plan failure classification](https://github.com/vl-ktk/ai-agent-engine/pull/4) merged as `b3e6bbea49bd5b4c5b491dde78c5d6dabcbbaaf1`. Parses at most 16 KiB of failure body, never shows raw provider diagnostics; preserves safe HTTP status, error code and parameter, body shape and sanitized request ID. Distinguishes quota/429, eligibility/403, invalid route/input, authentication/401 and temporary 503; applies to both text and native-tool streaming. No automatic repeated requests or paid API fallback. [CI run 43968801711](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43968801711): 3/3 PASS, **64/64 offline tests** each on Python 3.11/3.12/3.13; compilation and offline demo PASS. **The owner's live session was not used**; real quota errors, refresh and GPT native tool calls remain unverified. No production deployment.

**Completed since this decision: PR #3** [sign-out implementation](https://github.com/vl-ktk/ai-agent-engine/pull/3) merged into `main` as `270e06c6def0aa0098db998cf7808412c3cda85a`. Added explicit **manual-only** `python -m ai_agent_engine.auth logout`: fetch official OIDC discovery, restrict revocation to OpenAI HTTPS origin, POST refresh-token revocation, retry transient failures with short backoff, remove local session even when remote revocation is unconfirmed, preserve host ID and issued client ID, display a warning to disconnect from ChatGPT Settings on remote failure. [GitHub Actions run 43967444308](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43967444308) was 3/3 PASS, 53/53 offline tests each on Python 3.11, 3.12, 3.13, compile and offline demo PASS. **This is an offline PASS only:** no revocation or refresh was invoked on the owner's real saved session; no API bill or new resource.

**Owner workflow decision, 2026-11-17:** stop the repeated manual VS Code/Codespaces tasks on iPhone. The owner tried to run Task 4 from Safari; workspace tasks did not appear in the picker and one attempt showed Codespaces syncing, so **the live native-tool test has NOT happened**. The developer handles repository source, offline regression tests and CI autonomously, reporting only real results. Group any indispensable OAuth-bound GPT/Claude live acceptance into one short owner-assisted session later. Keep 1.6 and 1.7 explicitly unfinished; never turn an offline PASS into live PASS. Do not create paid infrastructure or copy tokens.

- **Prepared for the next owner-operated live test; NOT YET LIVE PASS:** [PR #2](https://github.com/vl-ktk/ai-agent-engine/pull/2) merged as `f9bfe0ffb0bb9dac600d647757ce2b30a99ea358`. GitHub Actions [run 43963803625](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43963803625) was 3/3 PASS on Python 3.11, 3.12 and 3.13; **47/47 offline unit tests on each version**, compile and offline demo PASS. New `.vscode/tasks.json` action `AI Agent Engine: 4. GPT writes and checks a safe test file` runs an explicit owner-triggered sandbox test. It uses the existing bounded Agent and exactly two native namespaced function tools: create a new text file and read it back in a disposable temporary directory. An independent file-content check is required to display PASS. No CI/live GPT inference ran; no terminal copy/paste needed.
- As a prerequisite to task 4, the pilot can refresh an expiring ChatGPT session with an OS-locked OAuth refresh grant, saved privately outside Git. This is a **partial, Linux-only implementation of 1.7**, not full lifecycle/revoke/VM coverage and not a completed live refresh test. If task 4 fails, owner should share only a screenshot of the safe error — never a token or OAuth URL. No API key or paid billing.


- **Live GPT Plus inference smoke-test PASS 2026-11-16.** [PR #1](https://github.com/vl-ktk/ai-agent-engine/pull/1) merged into development `main` code version `3e78ed4afbfca3e80bd763946f07f3b088ac2800`; offline GitHub Actions [run 43959657741](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43959657741) was **3/3 PASS** on Python 3.11, 3.12 and 3.13. More importantly, the owner opened the existing Codespace on iPhone, used task `AI Agent Engine: 1. Check ChatGPT sign-in` and saw `GRANTED`, then task `AI Agent Engine: 3. Send one GPT test request`; the screen displayed `ChatGPT model: gpt-6.1-sol` and `Completed GPT reply: Hello, world!` from the real streamed OpenAI Responses endpoint. The probe only returns after `response.completed`; no API key and no secrets in logs/Git. This verifies one actual response on ChatGPT plan allowance, **not** long-running throughput, usage-limit handling, refresh, safety or Claude. Roadmap 1.6 remains incomplete.
- Official OpenAI plan-usage guide: https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference — for the same approved OAuth account, use `GET https://api.openai.com/v1/models` to discover its available slugs; for inference use `POST https://api.openai.com/v1/responses` with `store:false`, `stream:true`, and only count `response.completed` as success. Handle `response.failed`, incomplete/interrupted streams and usage limits. Do not call undocumented ChatGPT backend endpoints or use a pay-as-you-go API key.
- A minimal live test should run **inside the existing Codespace** with its local credentials; never copy them to GitHub Actions, other connectors, messages or this public source. Access tokens expire around one hour; token refresh remains a separate unfinished 1.7 task.
- 1.6 also includes a later owner-operated official Claude Code `claude -p` test under the user's subscription, with no `ANTHROPIC_API_KEY` and no chargeable API calls, plus a permitted-integration review. Do not report it as tested.
- 1.7: safe access-token refresh/revocation and transfer to owner-approved VM; official guidance: https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions .
- **Current handoff:** The owner cannot paste commands from iPhone Safari, so `.vscode/tasks.json` offers click-only actions. They synced `main`, ran task 1 (the CLI reported ChatGPT plan permission `GRANTED`) and task 3 (real model `gpt-6.1-sol`, response `Hello, world!`). The saved access token was shown expiring `2026-11-16T21:51:31+00:00` and will need safe refresh under 1.7; never request or display raw OAuth tokens. The remote Codespace terminal remains inaccessible to the agent, but is accessible to the owner through VS Code Web. **Next developer step:** progress autonomous, no-credential work under phases 1.6–1.7 (offline edge-case/error handling, cross-platform auth lifecycle, tests and current documentation). Do not request another phone task. Later, arrange one combined, owner-initiated live acceptance of GPT native tools/refresh and official Claude Code `claude -p` under personal Pro (no `ANTHROPIC_API_KEY`) once everything is prepared and the owner chooses to do so. **No live result can be claimed before that session.**
- **No spending or new resources authorized.** The existing Codespace consumes the owner's free quota when running. Do not start paid API, paid VM or production service without explicit approval.

## Release and validation rules

The earlier manual GPT diagnostic passed GitHub Actions 3/3 and **a real owner-operated GPT smoke-test**. The new native tools test passed **47 offline tests per Python version** and awaits owner live verification. No production deployment occurred. The full 1.6 item remains open for Claude Code, usage-limit/error testing and integration review. Offline CI alone is not live proof. For code changes, read current `main`, implement in a branch or working copy, run tests and CI, recheck main immediately before committing/publishing, then document the exact code version and rollback ID here. Keep code/CLI and credentials separate; never surface OAuth URLs or tokens.
