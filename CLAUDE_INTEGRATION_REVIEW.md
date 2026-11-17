# AI Agent Engine — phase 1.6(в): Claude Code integration review

**Date:** 2026-11-17 (Europe/Kyiv)  
**Scope:** one owner, open-source personal agent engine, existing Codespace, possible future owner-controlled self-hosted VM. No public service or additional API billing is authorized.  
**Type:** documentation and architecture risk review. **Not** legal advice, a provider permission grant, or proof of continuous background reliability.

## 1. What was actually verified

- PR #10 merged into `main` at `282055c410e4379a868b88936b6969063cbd2182`; rollback baseline is `03bb0c3a8be6d486563ac2417186255dc51d6162`.
- Claude Code recorded **one owner-authorized LIVE run on 2026-11-17: 12/12 PASS** (GPT 8 stages; Claude 4 stages) inside the existing Codespace. The Claude probe verified a `claude.ai` first-party `pro` sign-in and one completed `claude -p` text response. The GPT sandbox file write/read cycle completed.
- The owner-provided LIVE result is documented in `HANDOFF.md` and PR #10; it is **not** an independently replayed long-running benchmark. PR #10 offline CI succeeded on four runners (Linux Python 3.11–3.13 and Windows Python 3.12). Last PR reported 120 offline tests.
- `ai_agent_engine/acceptance.py` preflights a first-party personal subscription and rejects provider/API-key routing variables. Its Claude probe uses the unmodified official executable with `-p`, JSON output, a temporary workdir, disabled tools, a strict MCP configuration, and no persisted session. It does **not** turn Claude into a general autonomous executor.
- **Not verified live:** sustained use under queue/concurrency, session renewal at expiry, exhaustion of Claude's plan quota, actual cost controls or Extra Usage setting via CLI, unattended restarts, token revocation, server migration, and anything resembling a third-party SaaS backend.

## 2. Current primary-source boundaries

1. Anthropic documents that `claude -p` and Agent SDK requests made using a Claude plan currently draw on subscription limits. This is an allowance-metering statement, not blanket approval for every integration or customer-facing product.
2. Anthropic describes subscription OAuth as intended for ordinary use of the official Claude Code and native apps. Developers building products/services interacting with Claude capabilities are directed to API-key authentication; third-party developers must not offer their own Claude.ai login, route subscription credentials on behalf of other users, or collect/intermediate Claude OAuth tokens.
3. Running Claude Code as a component of hosted product infrastructure has additional commercial-terms conditions; an end user may still sign in to the **unmodified** official binary with their own subscription. This does not automatically resolve whether an unattended personal orchestration service fits ordinary individual usage. Seek clarification from Anthropic before relying on that interpretation.
4. Claude Pro/Max subscription limits are shared with Claude Code; using an `ANTHROPIC_API_KEY` can switch to charged API usage. Authentication status alone does **not** prove Claude Extra Usage is disabled.
5. Separately, OpenAI documents eligible open-source personal agents on self-hosted VMs through Sign in with ChatGPT. That route has its own host IDs, scope, model, stream and token lifecycle requirements; it does not authorize any Claude integration.

**Primary sources checked on 2026-11-17:**

- Anthropic Claude Code legal/compliance: https://code.claude.com/docs/en/legal-and-compliance
- Anthropic plan + SDK/`claude -p` status: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- Anthropic Pro/Max/Ultra CLI usage and API-key warnings: https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-max-or-ultra-plan
- OpenAI OSS plan usage: https://developers.openai.com/siwc/token-sharing-open-source
- OpenAI self-hosted VMs: https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms

## 3. Scenario decisions (engineering recommendation, not a legal ruling)

| Scenario | Status | Why |
| --- | --- | --- |
| Owner manually uses the official Claude Code IDE/CLI in their Codespace under their own Pro plan | **GO for personal pilot** | Ordinary first-party CLI use; one real short `-p` test passed. Usage limits apply. |
| Owner calls unmodified official `claude -p` for occasional personal tasks from their own CLI | **Conditional pilot** | Technically proven for one text reply, but do not claim continuous reliability or unrestricted automatic orchestration. |
| AI Agent Engine runs Claude Pro as an unattended, long-lived background executor on a private VM | **HOLD / seek confirmation** | Intended OAuth limits for ordinary individual use vs. automated agent infrastructure are not conclusively addressed by one probe. Clarify authorization and usage before implementing a production scheduler on subscription credentials. |
| Hosted/multi-user software routes others' work through one Claude Pro account, intermediates their credentials or presents custom Claude.ai login | **NO-GO with subscription credentials** | Explicit Anthropic product/authentication restrictions; requires appropriate commercial arrangement and/or individually billed API-key route. |
| GPT engine via eligible ChatGPT-plan SIWC in an owner self-hosted VM | **Conditional, separate 1.7/4** | OpenAI documents the path; no real VM migration was verified. |

## 4. Mandatory safeguards for future architecture

- Keep provider adapters independent. The present personal owner-operated Claude Code pilot must **not** be silently promoted to a production/hosted backend.
- Never collect, extract, move, log, proxy, or store Claude OAuth/session tokens inside AI Agent Engine; authentication remains the official CLI's own flow and files.
- Never switch to Anthropic API keys, Console billing, gateway providers, credits or paid Extra Usage without **new explicit owner authorization**. Protect against inherited credential-routing environment variables.
- Require owner approval for any unattended Claude subscription workflow, product distribution, multi-user capability, remote deployment, and costs. Provider clarification may be needed before owner approval suffices.
- For eventual execution, design explicit run caps, timeouts, no automatic retry on quota errors, audit without prompt/credential leakage, proper cancellation, and a queue that works when Claude is unavailable. Test these offline first.
- The GPT SIWC pilot is already independent. Do not mix GPT and Claude credentials or move any credentials during this review.

## 5. Status and next checkpoint

**1.6(в) documented technical/legal review: completed with CONDITIONAL/HOLD decision for unattended Claude Pro orchestration.** The global 1.6 checkpoint remains **OPEN** until owner decides which permitted integration mode to use and reliability is validated where appropriate. Stages 1.7 (live token refresh/revocation, later owner-approved VM) and 1.8 (cost and GO/NO-GO) also remain open.

No API request, personal sign-in, extra live GPT/Claude call, VM creation, deployment, or production change was made for this documentation review.
