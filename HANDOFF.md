# HANDOFF — AI Agent Engine

**Updated:** 2026-11-16
**Source of truth:** https://github.com/vl-ktk/ai-agent-engine, branch `main`.
**Rollback baseline:** `715f651f0a9ca1403bdc6da320e3767e95a8260b` (before the browser-launch fix).
**Scope:** Only AI Agent Engine. KTK Social, ATHL and the separate local-AI project are unrelated.

## Current state

- Phase 1 of [IMPLEMENTATION_ROADMAP.md](./IMPLEMENTATION_ROADMAP.md) remains **incomplete**: a real ChatGPT login and a real authorized model request have **not** succeeded. This is not production.
- Existing Codespace: `laughing-disco-544rxwqgjxgcw74`. Python 3.12.11, user-approved Firefox ESR and private password-protected noVNC desktop on `127.0.0.1:6080` (GitHub-forwarded **Private**). GUI packages were installed in the current ephemeral Codespace only. No paid API calls.
- SIWC Python CLI: `python3 -m ai_agent_engine.auth login`, `status`, and `login --manual-browser`. Local OAuth callback binds only to `127.0.0.1`. Session secrets live in the user's Codespace config directory, **never** in GitHub, logs or this note.
- On the owner's iPhone, OAuth login reached the official `Connect AI Agent Engine to ChatGPT` consent page. They verified identity by ChatGPT push and pressed Connect, but the page remained loading; Firefox also displayed a **security-key/WebAuthn dialog**. No callback completed and `auth status` said `Not signed in on this computer`.
- Root cause confirmed for **one independent blocking bug**: the previous Python process (at diagnosis PID 50551) was in `do_wait`, waiting for its child `firefox-esr`, while the loopback callback socket was listening. Calling `webbrowser.open()` synchronously meant the Python callback wait never began until Firefox exited.
- Fix: `ai_agent_engine/siwc.py` now launches the OS browser on a daemon thread; the loopback callback loop and timeout proceed immediately. Browser-launch errors tell the operator to use `--manual-browser`. Offline regression test in `tests/test_siwc.py` simulates a `webbrowser.open` that waits for the browser to close.
- **Verified on the Codespace branch `fix/oauth-browser-nonblocking`:** `python3 -m unittest discover -s tests` **22/22 PASS** (0.483 s); `python3 -m compileall -q ai_agent_engine` PASS; `python3 -m ai_agent_engine` offline demo PASS (`status=done`, 3 steps); `git status --short` clean. This does **not** prove the separate browser WebAuthn/consent spinner is fixed.

## Next exact step

1. Ensure Codespace is on new `main` commit (`git switch main`, `git pull --ff-only`) after the fix is published.
2. Check whether the **old** OAuth login process is still stuck; if so stop **that exact verified PID** before retrying. Do not kill unrelated processes. A fresh `python3 -m ai_agent_engine.auth login` must be run in the Codespace while Firefox is running on its private desktop (not iPhone Safari's own host).
3. Owner completes OpenAI consent in desktop Firefox. If a security-key dialog appears again, handle it separately; don't assume the Python fix eliminates it. Recheck `python3 -m ai_agent_engine.auth status` and callback result without displaying tokens.
4. Only after real sign-in succeeds, proceed to the next phase-1 goal and confirm quota/terms before any live inference; no pay-as-you-go spending without separate owner authorization.

**Deployment:** Source code on GitHub only; no production server. **Rollback:** revert the auth/thread + regression commits with a new GitHub revert commit based on baseline above, without overwriting unrelated commits.
