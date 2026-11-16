# HANDOFF — AI Agent Engine

**Updated:** 2026-11-16
**Source of truth:** https://github.com/vl-ktk/ai-agent-engine, branch `main`.
**Last merged development code version:** `3e78ed4afbfca3e80bd763946f07f3b088ac2800` (manual-only GPT inference probe and VS Code Tasks; live model reply is still unverified).
**Rollback baseline before this one-click development merge:** `b942411c5d140a88df01f3393ae39969ec313b05`.
**Rollback baseline (before OAuth browser fix):** `715f651f0a9ca1403bdc6da320e3767e95a8260b`.
**Scope:** Only AI Agent Engine. Not KTK Social, ATHL, or the separate local-AI project.

## Verified milestone — real ChatGPT OAuth successful

- **2026-11-16: phase 1.5 real-user sign-in PASS.** The owner used the remote Firefox desktop in existing GitHub Codespace `laughing-disco-544rxwqgjxgcw74`, approved their ChatGPT push/consent, and reached the local `127.0.0.1` OAuth callback. No home PC was needed.
- Real `python3 -m ai_agent_engine.auth login` terminal result: `Identity verified` for the owner's iCloud account; `ChatGPT plan permission: GRANTED`.
- Immediately repeated `python3 -m ai_agent_engine.auth status`: displayed saved identity, `GRANTED`, and an access-token expiry approximately one hour from issuance. The account identifier is intentionally omitted from this handoff to minimize personal data.
- Local secrets, access and refresh tokens were **not** printed, moved to GitHub or sent in the chat. They are in the Codespace user's protected app-data directory outside this public repository. Credentials are not replicated to the current agent runtime.
- Existing nonblocking browser fix: `ai_agent_engine/siwc.py` launches Firefox on a daemon thread while the loopback server waits for the callback. Before live authentication, 22/22 offline unit tests, compileall and offline demo passed. No production deployment.

## Current limitations and next step — 1.6

- **No live GPT inference yet.** [PR #1](https://github.com/vl-ktk/ai-agent-engine/pull/1), branch `feat/siwc-model-probe`, was merged into development `main` at `3e78ed4afbfca3e80bd763946f07f3b088ac2800` after offline GitHub Actions [run 43959657741](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43959657741) **3/3 PASS** on Python 3.11, 3.12 and 3.13. New `.vscode/tasks.json` exposes three manual, click-only checks for an iPhone owner: account status, model catalog, and one minimal GPT response (the last consumes plan allowance only when chosen). CLI can also use `python3 -m ai_agent_engine.inference models` or `probe`. The branch/CI contain no secrets and never make provider calls. **The owner has NOT yet run the live GPT task**, so 1.6 remains open.
- Official OpenAI plan-usage guide: https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference — for the same approved OAuth account, use `GET https://api.openai.com/v1/models` to discover its available slugs; for inference use `POST https://api.openai.com/v1/responses` with `store:false`, `stream:true`, and only count `response.completed` as success. Handle `response.failed`, incomplete/interrupted streams and usage limits. Do not call undocumented ChatGPT backend endpoints or use a pay-as-you-go API key.
- A minimal live test should run **inside the existing Codespace** with its local credentials; never copy them to GitHub Actions, other connectors, messages or this public source. Access tokens expire around one hour; token refresh remains a separate unfinished 1.7 task.
- 1.6 also includes a later owner-operated official Claude Code `claude -p` test under the user's subscription, with no `ANTHROPIC_API_KEY` and no chargeable API calls, plus a permitted-integration review. Do not report it as tested.
- 1.7: safe access-token refresh/revocation and transfer to owner-approved VM; official guidance: https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions .
- **Current validation handoff:** The owner reopened the original Codespace in VS Code for the web, showed the integrated terminal on `main`, and verified a clean local `git status --short` (no output). On the iPhone, the owner cannot paste commands. The agent cannot access that private terminal directly. **Next owner action without typing:** in VS Code use Source Control → Pull (or Sync Changes) to receive the merged main; then Terminal → Run Task → `AI Agent Engine: 1. Check ChatGPT sign-in`; next choose Task 2 for model catalog and Task 3 for the minimal real reply. Report only non-secret status/output. A refresh is needed if the saved access token expires; implement safe refresh in 1.7 rather than sending tokens in chat.
- **No spending or new resources authorized.** The existing Codespace consumes the owner's free quota when running. Do not start paid API, paid VM or production service without explicit approval.

## Release and validation rules

This turn merged a manual-only inference diagnostic into development `main` after GitHub Actions passed 3/3; **no production deployment or live inference was performed**. Offline tests do not prove live access. Do not mark 1.6 complete until a real model response finishes. For code changes, read current `main`, implement in a branch or working copy, run tests and CI, recheck main immediately before committing/publishing, then document the exact code version and rollback ID here. Keep code/CLI and credentials separate; never surface OAuth URLs or tokens.