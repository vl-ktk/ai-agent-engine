# AI Agent Engine (early prototype)

Model-neutral, open-source agent runtime for personal self-hosted orchestration.

> **Status: offline agent demo + real ChatGPT OAuth login verified on 2026-11-16.**
> A real ChatGPT Plus GPT-6.1-sol reply **was verified by the owner** on 2026-11-16.
> The native-function sandbox tool cycle is prepared for a separate manual live test; it is **not yet live-verified**.
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

Credentials are saved outside this repository in the OS user's app-data directory (`%LOCALAPPDATA%\\AI Agent Engine` on Windows; `~/.config/ai-agent-engine` on Linux/macOS). Never copy those credentials to chat or GitHub. The one-account pilot does not silently switch identities. An explicit GPT diagnostic can refresh an expiring token using an OS-managed lock on Linux, macOS or Windows; an explicit `auth logout` command attempts remote refresh-token revocation and removes the local tokens. Neither operation has undergone an owner-operated live integration test; phase 1.7 remains open. On Windows the data directory relies on your Windows user-profile ACL; it is **not** a production-grade secret vault.

To sign out manually when the owner chooses to disconnect this installation, run `python3 -m ai_agent_engine.auth logout` (or `py -m ai_agent_engine.auth logout` on Windows). It attempts the OpenAI OIDC refresh-token revocation with limited retries, then deletes the local tokens while retaining the issued client registration and host ID for later sign-in. **This command has NOT been run against the owner's live account.** If remote revocation could not be confirmed, the CLI explicitly advises disconnecting the app in ChatGPT Settings. Never invoke it automatically.

**Limits:** The owner verified one completed streamed GPT answer using the ChatGPT plan allowance. Full native tool calling and Claude remain unverified (phase 1.6). Signing in itself does not start a paid OpenAI API call.



## Optional GPT checks in Codespaces (phase 1.6; owner live testing deferred)

This repository retains optional VS Code Tasks for when an owner-assisted live
acceptance is genuinely needed. **Do not try to run these on an iPhone now:**
the owner found Safari/VS Code Tasks unreliable, so all intermediate work uses
automated offline GitHub Actions. The live function-call check remains unverified.
For a later combined owner-assisted session:

1. Open the existing owner-authorized GitHub Codespace in VS Code for the web.
2. Once the branch with these files is available locally, select
   **Terminal → Run Task → AI Agent Engine: 1. Check ChatGPT sign-in**.
3. Optionally select **AI Agent Engine: 2. List available GPT models**.
4. Select **AI Agent Engine: 3. Send one GPT test request** to use a small amount
   of the ChatGPT account's plan allowance and print the response. This selects
   the first account-visible model, sends one short stateless streamed Responses
   request, and only declares success for a completed response event.
5. Select **AI Agent Engine: 4. GPT writes and checks a safe test file**
   to try the next milestone. This runs at most four ChatGPT model turns,
   exposes only two namespaced functions (`write_new_file` and `read_file`),
   verifies both calls against a real file in an automatically deleted
   temporary directory, and refuses to claim PASS without matching contents.
   The click may refresh the saved SIWC token if expired; OAuth refresh is
   protected by a bounded interprocess lock on Linux/macOS/Windows and never printed.
   The Windows locking path has only synthetic offline tests, not a native Windows
   OAuth acceptance. This is still an owner-controlled pilot, not production storage.

The tasks do not run on workspace open. They keep OAuth credentials in the
Codespace user's protected storage; no secrets are passed as task arguments
or uploaded to GitHub. Tasks 1–3 keep their original expiry-only behavior. The new explicit
sandbox test (task 4) attempts a safe, single-session token refresh as a
prerequisite. Live verification of refresh/revocation, native Windows locking and secure VM migration remain phase 1.7.
**The live request must be performed by the owner in the existing Codespace;
CI cannot validate it and does not possess credentials.**

## Cross-platform ChatGPT session locking (phase 1.7 — offline only)

Refresh and explicit manual logout share the same persistent
`.refresh.lock` inside the owner's private credential directory. POSIX uses
`fcntl.flock`; Windows uses `msvcrt.locking` on one byte, with bounded
waits and fail-closed errors. The file is never removed, avoiding races
through replacement lock inodes. No token text enters the lock file, logs,
source control or GitHub Actions. A failure to obtain the lock prevents
credential rotation and leaves the saved session unchanged.

**Validation:** Linux CI exercises the real POSIX lock and synthetic
Windows `msvcrt` calls; the actual Windows operating system, its profile
ACL protection, network-file-system behavior and a live OAuth refresh/revoke
were not tested. This code does **not** implement VM credential import.
For a future owner-approved VM, preserve a distinct persistent VM host ID,
complete OAuth locally, transfer selected credentials through a protected
SSH channel to the VM's local credential directory, and let the VM own later
refreshes; never publish or copy tokens via GitHub/CI. See the
[official OpenAI VM guide](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms).
No VM or transfer is authorized or performed yet.

## Future self-hosted VM session staging (phase 1.7 — offline only)

The owner may later move a previously authorized **single ChatGPT account**
from a trusted local host to an owner-approved, self-hosted VM. This has **not**
happened. The CLI below never connects to a host, performs OAuth, copies or
prints secrets, contacts the model or chooses a deployment.

OpenAI's [self-hosted VM guidance](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
requires a **separate persistent VM `ext_agent_host_id`** established before
import, a protected transfer over a secure channel such as SSH, reuse of the
issued OAuth registration and the VM taking over refresh afterward.

Only **on the future VM**, when the owner has explicitly approved that server:

```bash
python3 -m ai_agent_engine.vm_credentials prepare-host
python3 -m ai_agent_engine.vm_credentials check
```

These commands create (once) and inspect a **non-secret VM host ID**, not a
token. Never use a laptop's `host.json` to replace this file. Later, an
owner-controlled SSH stdin pipe carrying the existing protected source
`session.json` may be offered to:

- `python3 -m ai_agent_engine.vm_credentials validate-stdin` for **read-only
  dry-run** validation (requires prepared VM ID and an empty destination).
- `python3 -m ai_agent_engine.vm_credentials import-stdin --apply
  --confirm-vm-host <VM_URN_UUID>` for **explicit one-time import**, after
  review and separate owner authorization. It refuses mismatched VM IDs,
  source/destination ID collisions, conflicting issued client registrations,
  or overwriting a destination session. The persistent refresh/logout lock
  serializes import against token operations; the VM's own ID replaces only
  the source `ext_agent_host_id` metadata.

Do **not** pass OAuth tokens via command arguments, shell history, chat,
GitHub, workflow variables or issues. Do not send session files through
public deployment systems. The tool checks fields and structure but **cannot
prove a copied token is still valid, re-verify the original OAuth login, or
guarantee success of remote refresh/revocation**. Origin login must be
trusted; dry-run is not a live OAuth verification. The target credential
store uses local file permissions; VM disk encryption, user isolation,
backups and lifecycle hardening belong to separately authorized phase 4.
No actual transfer or target VM was attempted in phase 1.7.

## Safe GPT plan errors (phase 1.6 — offline tested)

The CLI and bounded sandbox model distinguish documented ChatGPT-plan errors:
quota exhausted (429), permission/policy rejection (401/403), unsupported
request (400), and temporary usage/routing unavailability (503). A failed
stream is never a completed answer or verified tool result. Error diagnostics
retain the HTTP status, safe structured code/parameter, body **shape** and
sanitized request ID when available; arbitrary error detail and tokens are
not shown. No automatic retry of limited or unsupported model requests and
no billing fallback are attempted. See the
[official error and recovery guide](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery).

This behavior is verified by synthetic offline tests only; the owner has not
triggered real quota, permission or tool failures. The full 1.6 checkpoint
remains open until a combined owner-approved live acceptance.

## Combined GPT + Claude Code acceptance (phase 1.6; not yet live tested)

One owner-operated pilot replaces repeated phone-based VS Code Tasks. It has no
auto-run, no scheduled job and no production integration. Only later, on a host
where the owner has already signed in to **both** personal accounts:

```bash
python3 -m ai_agent_engine.acceptance check
python3 -m ai_agent_engine.acceptance run-live
```

- `check` is read-only and makes **zero model requests**. It checks the local
  ChatGPT plan grant and the official `claude auth status` JSON; only a
  `claude.ai` authentication method is accepted. It does not refresh tokens.
  Any missing CLI, sign-in or alternate API/third-party authentication blocks.
- `run-live` is **explicitly opt-in** and first checks both logins. It then
  runs the existing GPT disposable-file native-tool test and exactly one
  official Claude Code `-p` text reply. The Claude run uses a temporary
  working directory, no saved session, a single turn, no built-in/MCP tools
  and no plugin/hook customization. Each run consumes **subscription quota**.
  If GPT fails, Claude is not invoked. No retries or paid-API fallback.
- API/proxy-related `ANTHROPIC_*` and `CLAUDE_CODE_*` authentication-routing
  environment overrides are rejected. The CLI stdout/stderr on errors is
  never dumped to logs; only a bounded, successful, expected reply counts.
  **Do not run this via public CI or with an API key.**
- The actual binary, Claude subscription login, subscription allowance,
  runtime compatibility and GPT native function-calls are **not yet tested
  in a live session**. No tokens or secrets are moved between hosts.
  Keep the owner-offered live session until the other prerequisites are ready.
- This is **a personal evaluation harness, not a deployed Claude backend**.
  Anthropic's [legal guidance](https://code.claude.com/docs/en/legal-and-compliance)
  distinguishes ordinary individual sign-in to unmodified Claude Code from
  embedding Claude in a product/service; commercial terms and API-key
  authentication may apply to the latter. Do not infer permission for
  unattended third-party subscription routing from this CLI test.
  Command flags: [official CLI reference](https://code.claude.com/docs/en/cli-reference).

## Development without the home PC (phase 1.5 preparation)

Source of truth is GitHub `vl-ktk/ai-agent-engine`, branch `main`.

- GitHub Actions [CI (offline)](.github/workflows/ci.yml) checks Python 3.11, 3.12 and 3.13 on every push or pull request to `main`: compile, unit tests, and the local scripted demo. No account credentials, AI requests, or deployment steps are present.
- Optional [Codespaces dev container](.devcontainer/devcontainer.json) supplies a remote Python 3.12 development environment. An owner-approved free-quota Codespace was **created and tested on 2026-11-16**: Python 3.12.11, 21/21 offline tests PASS. A password-protected noVNC desktop was also installed in that instance, reachable through one GitHub **Private** forwarded port; the real ChatGPT OAuth login and consent have since **passed** in this same-host desktop, and a live GPT model response passed. Native tool calls are still unverified. The GUI system packages were installed interactively and are not yet reproducible after container rebuild. GitHub Codespaces consumes personal account compute/storage quota, so stop the instance when idle. Details: [HANDOFF.md](./HANDOFF.md).
- These automated tests verify logic but cannot complete interactive OAuth, confirm user scopes or prove that a live GPT model answers.

## Project roadmap and architecture

- [Implementation roadmap — 11 phases and detailed checkpoints](./IMPLEMENTATION_ROADMAP.md).
- [Architecture decisions — orchestrators, roles, permissions, memory and owner control](./ARCHITECTURE_DECISIONS.md).
- [Current status and handoff](./HANDOFF.md).

This repository is the source of truth for AI Agent Engine. Other projects and
repositories remain separate; integrations require explicit owner authorization.

## Planned integrations (NOT implemented)

- Live-verify native GPT tool actions in the isolated test directory; complete
  owner-assisted acceptance of refresh/revocation and VM migration separately.
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
