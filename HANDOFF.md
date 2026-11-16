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
- `python -m unittest discover -s tests -v`: **21/21 PASS** in GitHub Codespaces on Python 3.12.11 (2026-11-16); tests include simulated callback, signed JWT validation, and manual-browser mode. Live OAuth remains untested.

## Not implemented

- Real GPT/Claude model calls, a **completed live ChatGPT OAuth session**, remote VPS, MCP, durable task queue, production isolation/permissions or deployment. GitHub Actions offline CI **is implemented and verified**; it is not production deployment.
- Token refresh/revocation and multi-account UI; this one-account login pilot uses a user-local credential file (on Windows protected by user-profile ACL, not a production vault).
- `SandboxFiles` is adequate for controlled demo only; hostile workloads require real OS/container confinement and race-safe file operations.
- No paid API subscription usage, VPS purchase, model inference or production change occurred. Owner approved **free Codespaces quota only**; Codespace usage draws from that quota.

## Cloud-only development transition (owner-approved direction, 2026-11-16)

- Owner is away from home and their Windows PC is powered off. Do not require that machine for development.
- Existing 11 global roadmap phases remain unchanged. Work under phase 1.5 adds subitems 1.5.A (reproducible cloud environment), 1.5.B (GitHub Actions CI), 1.5.C (real SIWC sign-in without home PC).
- Cloud CI **PASS 2026-11-16**: [run #43912994908](https://github.com/vl-ktk/ai-agent-engine/actions/runs/43912994908), commit `f619255`, 3/3 jobs (Python 3.11, 3.12, 3.13). Owner acceptance of roadmap 1.5.B remains pending.
- Cloud Codespaces actual test **PASS 2026-11-16**: created `laughing-disco-544rxwqgjxgcw74` from repo `main`, 2-core/8 GB RAM/32 GB disk; owner approved VS Code workspace trust. Python 3.12.11 verified; 21/21 offline unit tests PASS. Codespace **stopped** after checks, retained for restart. No code change inside Codespace.
- GitHub Free account and Codespaces $0 budget with 'Usage blocked' verified in GitHub account UI before creation; Codespaces uses the included monthly core-hour/storage quota. Codespace storage shown at 1.86 GB. No paid VM was procured.
- SIWC CLI supports `login --manual-browser` for a GUI browser **on the same VM host** (offline simulated tests only). The Codespace exists, but tested commands for `chromium`, `chromium-browser`, `google-chrome`, `firefox`, `Xvfb`, `x11vnc`, `websockify` were not found. No remote GUI or real OAuth credentials yet. Loopback `127.0.0.1/auth/callback` must resolve inside the browser's own host. A phone browser aimed at cloud CLI cannot reach its loopback server. Only install a secure GUI/browser after separate owner approval; never make its remote desktop public.

## Next work

1. Codespaces baseline done with owner approval, Python 3.12.11 and 21/21 offline tests PASS; machine stopped to conserve free quota. Next: agree specific secure browser/GUI packages and confirm before installing them via cloud UI. Never publish an unauthenticated remote desktop or put OAuth tokens/auth URLs/credentials into chat, GitHub, or logs.
2. Arrange an owner-operated browser and OAuth callback on the same host without requiring the powered-off home Windows. Confirm real OIDC identity validation and `chatgpt.tokens.use.direct` + `resource.invoke`. Keep 1.5 [ ] until this has happened.
3. Only after permission, test one actual GPT model request and official Claude Code `claude -p` under 1.6, assess limits and terms. No usage-billed API key without separate owner consent.
4. Stage 1.7 will harden secure session refresh/revocation and VM migration; stage 4 begins only after owner decision on costs.

**Production deployment:** None. **Rollback:** GitHub history; record current prior `main` SHA before each new commit and use a new revert commit rather than destructive rewriting. No external services or other projects are modified.
