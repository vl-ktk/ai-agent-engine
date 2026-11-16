# HANDOFF — AI Agent Engine

**Updated:** 2026-11-17
**Source of truth:** https://github.com/vl-ktk/ai-agent-engine, branch `main`.
**Last merged development code version:** `b3e6bbea49bd5b4c5b491dde78c5d6dabcbbaaf1` (PR #4 safe ChatGPT-plan error classification in inference and tool adapter; offline CI PASS, **live quota/error conditions NOT verified**). PR #3 logout and PR #2 native GPT test-file tools/Linux refresh remain in source; their live tests are still pending.
**Earlier proven GPT response code:** `3e78ed4afbfca3e80bd763946f07f3b088ac2800` — owner live `gpt-6.1-sol` PASS 2026-11-16.
**Rollback before GPT error-handling PR #4:** `93fca68992aac8b5d7e371a90864557f677b0052` (main HEAD before merge).
**Rollback before sign-out PR #3:** `9aa574dbdc18f0454386c2011c2a077dcc72e15d` (source main immediately before merge).
**Rollback to code before native GPT tool test:** `daaea985ddeab093b7acf7f9704eff5ee317a50c`.
**Rollback baseline before earlier inference merge:** `b942411c5d140a88df01f3393ae39969ec313b05`.
**Rollback baseline (before OAuth browser fix):** `715f651f0a9ca1403bdc6da320e3767e95a8260b`.
**Scope:** Only AI Agent Engine. Not KTK Social, ATHL, or the separate local-AI project.

## Verified milestone — real ChatGPT OAuth successful

- **2026-11-16: phase 1.5 real-user sign-in PASS.** The owner used the remote Firefox desktop in existing GitHub Codespace `laughing-disco-544rxwqgjxgcw74`, approved their ChatGPT push/consent, and reached the local `127.0.0.1` OAuth callback. No home PC was needed.
- Real `python3 -m ai_agent_engine.auth login` terminal result: `Identity verified` for the owner's iCloud account; `ChatGPT plan permission: GRANTED`.
- Immediately repeated `python3 -m ai_agent_engine.auth status`: displayed saved identity, `GRANTED`, and an access-token expiry approximately one hour from issuance. The account identifier is intentionally omitted from this handoff to minimize personal data.
- Local secrets, access and refresh tokens were **not** printed, moved to GitHub or sent in the chat. They are in the Codespace user's protected app-data directory outside this public repository. Credentials are not replicated to the current agent runtime.
- Existing nonblocking browser fix: `ai_agent_engine/siwc.py` launches Firefox on a daemon thread while the loopback server waits for the callback. Before live authentication, 22/22 offline unit tests, compileall and offline demo passed. No production deployment.

## Current in-progress change — combined personal acceptance

Branch `phase-1-6-combined-owner-acceptance` adds an **opt-in** single-script
pilot for the remaining GPT native function and official Claude Code login
checks. No login, CLI install, billing, model call or VM deployment was
performed by the developer. `check` only inspects local GPT permission and
the official Claude CLI auth-status response; `run-live` remains strictly
owner-triggered. New checks reject Claude API/Console and 3rd-party auth,
environment overrides, CLI errors or unexpected results, and disable Claude
tool/MCP access for the one-response test. Review and offline GitHub CI are
still required; **do not mark 1.6 completed**. Anthropic product/hosted-agent
usage may require commercial terms and API billing; this personal pilot grants
no blanket product approval.

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