# HANDOFF — AI Agent Engine

**Updated:** 2026-11-16
**Source of truth:** https://github.com/vl-ktk/ai-agent-engine, branch `main`.
**Last verified code version:** `7863cd2ee229e2daf16644dd8d9645da8c27a33a` (OAuth browser-launch fix plus regression test and prior handoff).
**Documentation version before this note:** `833ebadf48f4ba050c4fdd7caa01d87a72a8eec6`.
**Rollback baseline (before OAuth browser fix):** `715f651f0a9ca1403bdc6da320e3767e95a8260b`.
**Scope:** Only AI Agent Engine. Not KTK Social, ATHL, or the separate local-AI project.

## Verified milestone — real ChatGPT OAuth successful

- **2026-11-16: phase 1.5 real-user sign-in PASS.** The owner used the remote Firefox desktop in existing GitHub Codespace `laughing-disco-544rxwqgjxgcw74`, approved their ChatGPT push/consent, and reached the local `127.0.0.1` OAuth callback. No home PC was needed.
- Real `python3 -m ai_agent_engine.auth login` terminal result: `Identity verified` for the owner's iCloud account; `ChatGPT plan permission: GRANTED`.
- Immediately repeated `python3 -m ai_agent_engine.auth status`: displayed saved identity, `GRANTED`, and an access-token expiry approximately one hour from issuance. The account identifier is intentionally omitted from this handoff to minimize personal data.
- Local secrets, access and refresh tokens were **not** printed, moved to GitHub or sent in the chat. They are in the Codespace user's protected app-data directory outside this public repository. Credentials are not replicated to the current agent runtime.
- Existing nonblocking browser fix: `ai_agent_engine/siwc.py` launches Firefox on a daemon thread while the loopback server waits for the callback. Before live authentication, 22/22 offline unit tests, compileall and offline demo passed. No production deployment.

## Current limitations and next step — 1.6

- **No live GPT inference yet.** Present client supports only `login` and `status`. No model catalog, Responses streaming adapter, or model/agent call is implemented.
- Official OpenAI plan-usage guide: https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference — for the same approved OAuth account, use `GET https://api.openai.com/v1/models` to discover its available slugs; for inference use `POST https://api.openai.com/v1/responses` with `store:false`, `stream:true`, and only count `response.completed` as success. Handle `response.failed`, incomplete/interrupted streams and usage limits. Do not call undocumented ChatGPT backend endpoints or use a pay-as-you-go API key.
- A minimal live test should run **inside the existing Codespace** with its local credentials; never copy them to GitHub Actions, other connectors, messages or this public source. Access tokens expire around one hour; token refresh remains a separate unfinished 1.7 task.
- 1.6 also includes a later owner-operated official Claude Code `claude -p` test under the user's subscription, with no `ANTHROPIC_API_KEY` and no chargeable API calls, plus a permitted-integration review. Do not report it as tested.
- 1.7: safe access-token refresh/revocation and transfer to owner-approved VM; official guidance: https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions .
- **Current blocker:** In this subsequent turn, terminal/executor is unavailable; browser extension reports no originating browser context. GitHub source is reachable through the connected GitHub API, but the private Codespace token and shell are not. The first GPT request cannot be truthfully executed or verified until an authorized remote terminal is accessible.
- **No spending or new resources authorized.** The existing Codespace consumes the owner's free quota when running. Do not start paid API, paid VM or production service without explicit approval.

## Release and validation rules

Only documentation changed in the present turn, reflecting the independently observed live OAuth outcome. Do not mark 1.6 complete until a real model response finishes. For code changes, read current `main`, implement in a branch or working copy, run tests and CI, recheck main immediately before committing/publishing, then document the exact code version and rollback ID here. Keep code/CLI and credentials separate; never surface OAuth URLs or tokens.