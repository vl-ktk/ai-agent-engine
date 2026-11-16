# HANDOFF — AI Agent Engine

**Date:** 2026-11-16
**Source of truth:** `https://github.com/vl-ktk/ai-agent-engine`, branch `main`.
**Current scope:** Minimal offline agent-engine MVP for the wider AI Development System.

## Implemented and checked

- Provider-neutral model protocol, bounded agent loop, explicit tool allowlist.
- Isolated demo filesystem tools, new-file-only writes, safe failure statuses and
  content-free audit events.
- Offline `ScriptedModel` fixture (not an LLM).
- `python -m ai_agent_engine`: PASS, 3 model/tool steps.
- `python -m unittest discover -s tests -v`: PASS, 10/10 tests on 2026-11-16.
- Public GitHub repository with MIT license, no account credentials or project data.

## Not implemented

- Real GPT or Claude integration, user OAuth, remote VM, MCP, durable task queue,
  production sandbox/permissions, CI or deployment.
- This demo's `SandboxFiles` is not adequate isolation for hostile/multi-tenant
  execution or production workloads.

## Next work

1. Project owner reviews this MVP and accepts roadmap item 1.4.
2. Follow separate roadmap item 1.5 for local Sign in with ChatGPT authorization;
   keep all credentials out of Git and logs.
3. Only after authorization and explicit permission, run the live model tests
   from item 1.6. Do not enable usage-based API billing.

**Production deployment:** None. **Rollback:** revert the latest source commit
or browse the previous repository commit through GitHub history. No external
services or user projects were modified.
