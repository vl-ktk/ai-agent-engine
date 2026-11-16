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

**Run on your own Windows PC**, with Python 3.11+ and a browser installed. From the checked-out repository directory:

```powershell
py -m pip install -r requirements-siwc.txt
py -m ai_agent_engine.auth login
py -m ai_agent_engine.auth status
```

On Linux/macOS replace `py` with `python3`. During `login`, approve the app and requested ChatGPT plan usage in your browser. The callback works only on `127.0.0.1` **on the same computer**. Do not run login on a remote VM, in ChatGPT's sandbox, or in an unrelated browser on your phone.

Credentials are saved outside this repository in the OS user's app-data directory (`%LOCALAPPDATA%\\AI Agent Engine` on Windows; `~/.config/ai-agent-engine` on Linux/macOS). Never copy those credentials to chat or GitHub. The one-account pilot does not silently switch identities; it does not yet refresh or revoke tokens (phase 1.7). On Windows the data directory relies on your Windows user-profile ACL; it is **not** a production-grade secret vault.

**Limits:** Successful local code/tests do not prove that OpenAI will grant the required scope to your account. OAuth remains unverified until you actually complete `login`; model calls and subscription quota checks belong to phase 1.6. Signing in does not itself start a paid OpenAI API call.


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
