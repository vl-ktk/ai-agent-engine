# HANDOFF — AI Agent Engine

**Date:** 2026-11-16
**Source of truth:** https://github.com/vl-ktk/ai-agent-engine, branch `main`.
**Scope:** Independent open-source AI agent engine and future orchestration platform. Do not edit, synchronize back to, or treat another project's repository as a source of truth. A one-time transfer of agreed high-level logic has been recorded in this repository.

## Architecture and plan

- [IMPLEMENTATION_ROADMAP.md](./IMPLEMENTATION_ROADMAP.md): one official 11-phase checklist and detailed tasks. Phase 1 remains open.
- [ARCHITECTURE_DECISIONS.md](./ARCHITECTURE_DECISIONS.md): agreed runtime, roles, server candidate, ownership, QA cycle, project isolation, memory and future interactive agent city.
- These documents describe **plans and decisions**, not implemented software. Other projects are independent and are never connected by implication.

## Implemented and checked

- Public MIT repository `vl-ktk/ai-agent-engine`, `main`.
- Provider-neutral Python model protocol, bounded agent loop, explicit tool allowlist.
- Isolated demo filesystem tools, new-file-only writes, safe failure statuses and content-free audit events.
- Offline `ScriptedModel` fixture (**not** a live LLM).
- `python -m ai_agent_engine`: PASS, 3 model/tool steps (2026-11-16).
- `python -m unittest discover -s tests -v`: PASS, 10/10 tests (2026-11-16).
- Plan 1.1–1.3: documentary checks complete, subject to future official-policy updates.
- Plan 3.1–3.2: public code source and offline prototype technically checked.
- Plan 1.4: **accepted by owner** on 2026-11-16 after the 10-test offline prototype review.
- Phase 1.5 code: local SIWC dynamic registration, stable host ID, loopback callback, state/nonce/PKCE, signed ID-token check using JWKS, scopes, protected per-user file storage; OAuth has **not** been executed using a real account.
- `python -m unittest discover -s tests -v`: **19/19 PASS** after adding offline SIWC tests, including a simulated browser callback and signed JWT validation (2026-11-16).

## Not implemented

- Real GPT/Claude model calls, a **completed live ChatGPT OAuth session**, remote VPS, MCP, durable task queue, production isolation/permissions, CI or deployment.
- Token refresh/revocation and multi-account UI; this one-account login pilot uses a user-local credential file (on Windows protected by user-profile ACL, not a production vault).
- `SandboxFiles` is adequate for controlled demo only; hostile workloads require real OS/container confinement and race-safe file operations.
- No billing, API subscription usage, VPS purchase, model inference or production change has been authorized or carried out by this repository transfer.

## Next work

1. Owner runs the new CLI on their **own Windows computer**: `py -m pip install -r requirements-siwc.txt`, then `py -m ai_agent_engine.auth login` and `py -m ai_agent_engine.auth status`. Do not copy OAuth tokens, authentication URLs or credential files into chat.
2. Confirm that the real OIDC identity validation passed and `chatgpt.tokens.use.direct` + `resource.invoke` were granted. Keep 1.5 [ ] until this has happened.
3. Only after permission, test one actual GPT model request and official Claude Code `claude -p` under 1.6, assess limits and terms. No usage-billed API key without separate owner consent.
4. Stage 1.7 will harden secure session refresh/revocation and VM migration; stage 4 begins only after owner decision on costs.

**Production deployment:** None. **Rollback:** GitHub history; record current prior `main` SHA before each new commit and use a new revert commit rather than destructive rewriting. No external services or other projects are modified.
