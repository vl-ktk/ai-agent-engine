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

## Cloud-only development transition (owner-approved direction, 2026-11-16)

- Owner is away from home and their Windows PC is powered off. Do not require that machine for development.
- Existing 11 global roadmap phases remain unchanged. Work under phase 1.5 adds subitems 1.5.A (reproducible cloud environment), 1.5.B (GitHub Actions CI), 1.5.C (real SIWC sign-in without home PC).
- Cloud CI first live run **PASS 2026-11-16**: [run #1](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43905219025), commit `6587ce9`, 3/3 jobs (Python 3.11, 3.12, 3.13), each dependency install/compile/unit tests/offline demo PASS. Roadmap 1.5.B remains [ ] until owner accepts; 1.5.A has config only, Codespaces not launched.
- GitHub-hosted standard runners for the public repository are free; Codespaces has its own limited free quota and has **not** been started here. No paid VM has been procured.
- SIWC still requires loopback `127.0.0.1/auth/callback` in the browser's host. A phone browser pointed at cloud CLI cannot reach its loopback server. A cloud VM with a browser on that same VM may work but has not been tested or rented; do not claim real login success.

## Next work

1. Cloud CI is now verified PASS; optional clean Codespaces preview still needs owner permission if quota/billing may apply. Do not launch potentially metered Codespaces without owner approval. Do not copy OAuth tokens, authentication URLs or credential files into chat.
2. Arrange an owner-operated browser and OAuth callback on the same host without requiring the powered-off home Windows. Confirm real OIDC identity validation and `chatgpt.tokens.use.direct` + `resource.invoke`. Keep 1.5 [ ] until this has happened.
3. Only after permission, test one actual GPT model request and official Claude Code `claude -p` under 1.6, assess limits and terms. No usage-billed API key without separate owner consent.
4. Stage 1.7 will harden secure session refresh/revocation and VM migration; stage 4 begins only after owner decision on costs.

**Production deployment:** None. **Rollback:** GitHub history; record current prior `main` SHA before each new commit and use a new revert commit rather than destructive rewriting. No external services or other projects are modified.
