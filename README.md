# AI Agent Engine (early prototype)

Model-neutral, open-source agent runtime for personal self-hosted orchestration.

> **Status: offline agent demo + real ChatGPT OAuth login verified on 2026-11-16.**
> One-click GPT model probe is available for a manual live test; a completed real model answer is **not yet verified**.
> No Claude inference, server deployment or production access exists.

## What works in the prototype

- Generic model interface and bounded model/tool interaction loop.
- Strict allowlist of tools: arbitrary shell or network actions are unavailable.
- Scoped file tools operating only in an isolated, pre-existing test directory.
- New-file-only writes; existing files cannot be overwritten.
- Explicit step limit, blocked/failed/done statuses and content-free audit events.
- Reproducible offline model fixture demonstrating file creation and retrieval.

## Demo and tests

Requires Python 3.11+; no third-party packages, secrets or network connection.

```bash
python -m ai_agent_engine
python -m unittest discover -s tests -v
```

The demo uses a disposable temporary directory and does not touch working projects.

## Sign in with ChatGPT — local pilot (phase 1.5)

The OAuth flow follows the [official OSS sign-in guide](https://developers.openai.com/siwc/token-sharing-open-source/sign-in): dynamic client registration, loopback callback, PKCE, signed OpenID identity verification and explicit ChatGPT plan permission check.

**Existing local pilot:** run on a computer where Python 3.11+ and the browser are available **on the same host**. It is not tied to the owner's powered-off home Windows. From the checked-out repository directory:

```powershell
py -m pip install -r requirements-siwc.txt
py -m ai_agent_engine.auth login
py -m ai_agent_engine.auth status
```

On Linux/macOS replace `py` with `python3`. For an **interactive remote desktop**, use `python3 -m ai_agent_engine.auth login --manual-browser` in the VM terminal and open the displayed link in a browser running **inside that same remote desktop**. The URL is sensitive and must never enter chat, issue comments, workflow logs or public terminals. This does **not** enable signing in through Safari directly on a different phone. This same-host remote desktop flow successfully completed live OpenAI OAuth consent on 2026-11-16.

During `login`, approve the app and requested ChatGPT plan usage in your browser. The callback works only on `127.0.0.1` **on the same computer**. An unrelated phone browser cannot complete a loopback callback for a remote CLI. The owner completed a live same-host remote Firefox/OAuth callback on the GitHub Codespace. This does **not** authorize paid hosting.

Credentials are saved outside this repository in the OS user's app-data directory (`%LOCALAPPDATA%\\AI Agent Engine` on Windows; `~/.config/ai-agent-engine` on Linux/macOS). Never copy those credentials to chat or GitHub. The one-account pilot does not silently switch identities; it does not yet refresh or revoke tokens (phase 1.7). On Windows the data directory relies on your Windows user-profile ACL; it is **not** a production-grade secret vault.

**Limits:** The real account granted ChatGPT plan permission in the initial live login. A completed model answer and subscription usage remain unverified (phase 1.6). Signing in does not itself start a paid OpenAI API call.



## One-click GPT check in Codespaces (phase 1.6, pending live verification)

This repository includes voluntary VS Code Tasks so the owner can run the GPT
checks by tapping menus on an iPhone without typing or pasting shell commands:

1. Open the existing owner-authorized GitHub Codespace in VS Code for the web.
2. Once the branch with these files is available locally, select
   **Terminal → Run Task → AI Agent Engine: 1. Check ChatGPT sign-in**.
3. Optionally select **AI Agent Engine: 2. List available GPT models**.
4. Select **AI Agent Engine: 3. Send one GPT test request** to use a small amount
   of the ChatGPT account's plan allowance and print the response. This selects
   the first account-visible model, sends one short stateless streamed Responses
   request, and only declares success for a completed response event.

The tasks do not run on workspace open. They keep OAuth credentials in the
Codespace user's protected storage; no secrets are passed as task arguments
or uploaded to GitHub. If the access token has expired, the diagnostic reports
that condition; automatic token refresh remains a separate task (phase 1.7).
**The live request must be performed by the owner in the existing Codespace;
CI cannot validate it and does not possess credentials.**

## Development without the home PC (phase 1.5 preparation)

Source of truth is GitHub `vl-ktk/ai-agent-engine`, branch `main`.

- GitHub Actions [CI (offline)](.github/workflows/ci.yml) checks Python 3.11, 3.12 and 3.13 on every push or pull request to `main`: compile, unit tests, and the local scripted demo. No account credentials, AI requests, or deployment steps are present.
- Optional [Codespaces dev container](.devcontainer/devcontainer.json) supplies a remote Python 3.12 development environment. An owner-approved free-quota Codespace was **created and tested on 2026-11-16**: Python 3.12.11, 21/21 offline tests PASS. A password-protected noVNC desktop was also installed in that instance, reachable through one GitHub **Private** forwarded port; the real ChatGPT OAuth login and consent have since **passed** in this same-host desktop, while GPT inference is still pending. The GUI system packages were installed interactively and are not yet reproducible after container rebuild. GitHub Codespaces consumes personal account compute/storage quota, so stop the instance when idle. Details: [HANDOFF.md](./HANDOFF.md).
- These automated tests verify logic but cannot complete interactive OAuth, confirm user scopes or prove that a live GPT model answers.

## Project roadmap and architecture

- [Implementation roadmap — 11 phases and detailed checkpoints](./IMPLEMENTATION_ROADMAP.md).
- [Architecture decisions — orchestrators, roles, permissions, memory and owner control](./ARCHITECTURE_DECISIONS.md).
- [Current status and handoff](./HANDOFF.md).

This repository is the source of truth for AI Agent Engine. Other projects and
repositories remain separate; integrations require explicit owner authorization.

## Planned integrations (NOT implemented)

- Verify a real GPT model response using the already-granted ChatGPT subscription; later implement refresh/revocation and self-hosted VM migration.
- Claude Code installed and authorized by the actual user through Anthropic's
  official sign-in flow, after confirming the intended use is permitted.
- MCP, durable queue, permissions, persistent memory and safe service sandbox.
- User-requested production changes only after review and explicit approval.

## Security boundaries

Do not store credentials, personal project files or logs in this repository.
The current filesystem confinement is sufficient for this **controlled demo**,
not hostile multi-tenant execution; production needs OS/container isolation and
race-resistant filesystem access. The runtime must not execute arbitrary LLM
instructions as shell commands. Integration tests requiring credentials must be
performed outside the repository with secrets excluded from version control.

## License

MIT. See `LICENSE` in the GitHub repository after initialization.
