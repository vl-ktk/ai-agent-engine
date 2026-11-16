# AI Agent Engine (early prototype)

Model-neutral, open-source agent runtime for personal self-hosted orchestration.

> **Status: offline agent demo + locally testable Sign in with ChatGPT client.**
> OAuth integration is implemented but **no real account sign-in has been verified**.
> No GPT/Claude inference, server deployment or production access exists.

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

On Linux/macOS replace `py` with `python3`. For an **interactive remote desktop**, use `python3 -m ai_agent_engine.auth login --manual-browser` in the VM terminal and open the displayed link in a browser running **inside that same remote desktop**. The URL is sensitive and must never enter chat, issue comments, workflow logs or public terminals. This does **not** enable signing in through Safari directly on a different phone. This manual mode is tested only with a simulated callback, not with OpenAI's live authorization.

During `login`, approve the app and requested ChatGPT plan usage in your browser. The callback works only on `127.0.0.1` **on the same computer**. An unrelated phone browser cannot complete a loopback callback for a remote CLI. A remote GUI browser running on the same virtual machine as the callback may be evaluated separately, but is **not yet tested or authorized for paid hosting**.

Credentials are saved outside this repository in the OS user's app-data directory (`%LOCALAPPDATA%\\AI Agent Engine` on Windows; `~/.config/ai-agent-engine` on Linux/macOS). Never copy those credentials to chat or GitHub. The one-account pilot does not silently switch identities; it does not yet refresh or revoke tokens (phase 1.7). On Windows the data directory relies on your Windows user-profile ACL; it is **not** a production-grade secret vault.

**Limits:** Successful local code/tests do not prove that OpenAI will grant the required scope to your account. OAuth remains unverified until you actually complete `login`; model calls and subscription quota checks belong to phase 1.6. Signing in does not itself start a paid OpenAI API call.


## Development without the home PC (phase 1.5 preparation)

Source of truth is GitHub `vl-ktk/ai-agent-engine`, branch `main`.

- GitHub Actions [CI (offline)](.github/workflows/ci.yml) checks Python 3.11, 3.12 and 3.13 on every push or pull request to `main`: compile, unit tests, and the local scripted demo. No account credentials, AI requests, or deployment steps are present.
- Optional [Codespaces dev container](.devcontainer/devcontainer.json) supplies a remote Python 3.12 development environment. **It is configuration only; we have not launched a Codespace.** GitHub Codespaces consumes personal account compute/storage quota and may cost money if over limit with enabled billing. Get owner's consent before starting a metered environment.
- These automated tests verify logic but cannot complete interactive OAuth, confirm user scopes or prove that a live GPT model answers.

## Project roadmap and architecture

- [Implementation roadmap — 11 phases and detailed checkpoints](./IMPLEMENTATION_ROADMAP.md).
- [Architecture decisions — orchestrators, roles, permissions, memory and owner control](./ARCHITECTURE_DECISIONS.md).
- [Current status and handoff](./HANDOFF.md).

This repository is the source of truth for AI Agent Engine. Other projects and
repositories remain separate; integrations require explicit owner authorization.

## Planned integrations (NOT implemented)

- Complete and verify real Sign in with ChatGPT authorization on the owner's PC; later refresh/revocation and self-hosted VM transfer.
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
