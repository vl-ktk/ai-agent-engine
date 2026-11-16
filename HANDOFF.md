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
- Plan 1.4: implementation/test evidence exists but **awaits owner acceptance**; do not mark complete automatically.

## Not implemented

- Real GPT/Claude integration, Sign in with ChatGPT OAuth, remote VPS, MCP, durable task queue, production isolation/permissions, CI or deployment.
- `SandboxFiles` is adequate for controlled demo only; hostile workloads require real OS/container confinement and race-safe file operations.
- No billing, API subscription usage, VPS purchase, model inference or production change has been authorized or carried out by this repository transfer.

## Next work

1. Obtain explicit owner acceptance of 1.4.
2. Proceed with 1.5: local Sign in with ChatGPT flow, PKCE, consent, host identity, secure token handling; never put secrets in Git or chat.
3. After authorizing live model tests, check 1.6 GPT via valid SIWC + official Claude Code CLI and quantify real limits and compatibility. Do not enable paid API billing without consent.
4. Only after owner decision on later stages: VPS, durable system orchestrator, tools, MCP, panel and pilot projects.

**Production deployment:** None. **Rollback:** GitHub history; record current prior `main` SHA before each new commit and use a new revert commit rather than destructive rewriting. No external services or other projects are modified.
