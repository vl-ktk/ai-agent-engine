# AI Agent Engine (early prototype)

Model-neutral, open-source agent runtime for personal self-hosted orchestration.

> **Status: offline agent demo + real ChatGPT OAuth login verified on 2026-11-16.**
> A real ChatGPT Plus GPT-6.1-sol reply **was verified by the owner** on 2026-11-16.
> **2026-11-17: one combined live acceptance passed all 12 stages** — GPT `gpt-6.1-sol` reply plus native
> write/read sandbox tool calls verified on disk, and one official Claude Code `claude -p` reply under the
> owner's personal Claude Pro sign-in (no API key). No server deployment or production access exists.

**Phase 1.8 GO/NO-GO (2026-11-17):** [assessment and Stage 2 gates](./PHASE1_GO_NOGO.md).
Offline architecture work may proceed; unattended VM or Claude Pro operation remains on hold pending live and policy checks.

**Stage 3.7:** [technical engine audit and release boundaries](./STAGE3_ENGINE_AUDIT.md).
The Python runtime is suitable for offline experimental development, **not**
for autonomous production deployment. The agent rejects non-string/blank or
oversized final responses and rejects prompts above 65,536 characters before
invoking a model; both behaviors are covered by offline regression tests.

**Stage 4.1:** [controller VPS selection review](./STAGE4_VPS_SELECTION.md)
is a recommendation awaiting owner approval and Stage 1 VM gates. No server
has been purchased, configured, or deployed.

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

## Installable core (stage 3.6; offline)

The public core is a Python 3.11+ package with no third-party runtime
dependencies. The build backend is pinned in `pyproject.toml` to
`setuptools==84.2.1`; Python package metadata uses an explicit preview version.
From a clean checkout:

```bash
python -m pip install --no-deps .
python -m ai_agent_engine
```

The same installed demo also runs **outside the source directory**. Only when
working on the owner-operated OAuth flow, install the separate optional extras
with `python -m pip install '.[siwc]'` (or use `requirements-siwc.txt` for
the existing tests). These optional dependencies use a version range and are
**not a fully locked, byte-for-byte reproducible environment**. Installation
does not sign in, contact AI providers, create a VM, or deploy services.

CI builds on the original Linux/Windows Python matrix, then installs the
packaged core and runs its demo from the runner's temporary directory. It also
checks Git-tracked files (including force-added ignored files) for common
credential paths, key patterns, symlinks and unexpected binary files through
`scripts/check_public_source.py`. The scanner is a narrow defense in depth:
it cannot prove there are no undisclosed tokens or credentials in public code.

## Unified agent roles and task sessions (phase 3.3; offline-tested)

The same provider-neutral `Agent` model/tool loop is now available through
`AgentSession` for both `AgentRole.EXECUTOR` and `AgentRole.SUPERVISOR`. A session
binds one validated `project_id`, one `agent_id`, trusted role instructions,
an explicitly supplied set of tools, and a step limit. Separate sessions do
not inherit chat history; each session accepts exactly one task. The role is
*descriptive*, never an automatic permission or tool grant.

`StopToken.request_stop()` cooperatively cancels a pending session before its
next model call or tool action, returning `cancelled` with a content-free audit
event. It **cannot preempt an already running provider HTTP request or local
tool call**; process-level cancellation and persistent queues belong to later
stages. The existing `Agent.run(prompt)` contract still works unchanged.

The owner-only GPT sandbox tool-cycle now uses this same `AgentSession` path
through its existing `OpenAIPlanModel` protocol adapter. This route was
retested **offline with a fake transport only**; the earlier owner's live
12/12 check predates this change. This is **not** a general permission to
operate a supervisor with GPT/Claude on a server. Full provider adapters,
long-lived orchestration, project access policies and memory belong to later
roadmap stages. No new API usage, credentials, permissions or deployment are
introduced by phase 3.3.

## Safe tool registration and audit (phase 3.4; offline-tested)

`ToolRegistry` registers trusted functions once, then exposes **only** names
granted to the current `Agent` or `AgentSession`. A descriptive `AgentRole`
never adds privileges. Unknown functions and untrusted function names are
blocked without echoing attacker-supplied identifiers in the audit.

Each `Tool` declares exact `required_args` and `optional_args`, plus bounded
`max_argument_bytes` and `max_output_chars`. By default no arguments are
accepted. Only UTF-8 string arguments matching the declared schema are sent
to executors, as a defensive copy. Malformed inputs are blocked **before**
the executor runs; tool exceptions are redacted; oversized tool results never
enter the next model turn. A session has at most 20 model/tool steps, with
a configurable lower limit. Audit entries include the step, a safe outcome
(`tool_ok`, `invalid_tool_arguments`, `tool_error`, etc.), a registered tool
name when applicable, trusted project/agent IDs and UTC timestamp — **not**
argument values, file contents, token material or exception messages.

`SandboxFiles` grants only `read_file` and new-file-only `write_new_file`
within a caller-supplied pre-existing directory. Relative paths, existing
subdirectories and bounded UTF-8 files are permitted; traversal, Windows-style
absolute paths, symlinks, overwrites and excessive read/write payloads are
rejected. On POSIX it uses directory-descriptor-relative opens with
`O_NOFOLLOW` for individual path components. On Windows it uses canonical
path/symlink preflights in a directory trusted to the current account.
**This is not an OS process sandbox**: it does not isolate hostile processes
running as the same OS user or defend Windows path checks from concurrent
changes by such a process. Real per-project OS permissions, durable audit,
production policy and execution isolation belong to later phases.

The owner GPT sandbox test preserves its existing function schemas and still
uses only a disposable temporary directory; its new shared argument checks
were exercised offline with a synthetic transport. No paid provider request,
credential handling, server deployment or real project access is part of 3.4.

## Provider-neutral model routing (phase 3.5; offline-tested)

The `ModelRouter` selects one trusted `ProviderSpec` by its explicit provider
name and allowlisted model ID. There is **no automatic provider preference,
fallback, discovery, retry or paid API selection**. Factories are supplied by
the owner and called lazily only after the route and tool capability are
checked. A `ProviderSpec` declares `supports_tools`; a text-only provider is
rejected **before** its factory is called if a session has granted tools.
Every selected adapter implements the same `Model.complete(history, tools)`
protocol and runs through the existing `AgentSession` and `ToolRegistry`.

`TextOnlyModel` wraps any trusted, synchronous text-response function and
allows no tool calls. It accepts only the one-task initial system/user history,
rejects empty or oversized answers, and leaves model errors to the existing
redacted audit. The adapter does not itself execute a CLI or read any tokens.
For example, the following demonstrates two *offline* model routes, not an
actual OpenAI or Anthropic connection:

```python
from ai_agent_engine import (
    AgentRole, ModelRouter, ProviderSpec, SessionContext, TextOnlyModel,
)

router = ModelRouter((
    ProviderSpec("example_a", ("text-a",), lambda model: TextOnlyModel(lambda history: "A")),
    ProviderSpec("example_b", ("text-b",), lambda model: TextOnlyModel(lambda history: "B")),
))
session = router.new_session(
    provider="example_b", model_id="text-b",
    context=SessionContext("demo", "owner-task", AgentRole.EXECUTOR),
)
assert session.run("One safe task").answer == "B"
```

The existing `OpenAIPlanModel` is compatible with `ModelRouter`, including
native sandbox tool calls, as verified using **fake HTTP responses**. The
official Claude Code CLI currently has only its earlier, owner-triggered
acceptance probe; it is **not** wired as a general task executor. Actual
GPT/Claude runtime integration, credentials, session lifecycle, quota fallback,
user authorization and provider-specific tool translation remain for stage 6
and later explicit owner decisions. This phase sends **zero** live requests.

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

Credentials are saved outside this repository in the OS user's app-data directory (`%LOCALAPPDATA%\\AI Agent Engine` on Windows; `~/.config/ai-agent-engine` on Linux/macOS). Never copy those credentials to chat or GitHub. The one-account pilot does not silently switch identities. Owner-triggered GPT model commands (`models`, `probe`, `ask`, acceptance, sandbox tool-cycle) check whether the saved token needs renewal, using the same OS-managed refresh lock on Linux, macOS or Windows. No background refresh task or scheduled paid model request is installed. An explicit `auth logout` command attempts remote refresh-token revocation and removes the local tokens. Real refresh/revocation has **not** undergone an owner-operated live integration test; phase 1.7 remains open. On Windows the data directory relies on your Windows user-profile ACL; it is **not** a production-grade secret vault.

To sign out manually when the owner chooses to disconnect this installation, run `python3 -m ai_agent_engine.auth logout` (or `py -m ai_agent_engine.auth logout` on Windows). It attempts the OpenAI OIDC refresh-token revocation with limited retries, then deletes the local tokens while retaining the issued client registration and host ID for later sign-in. **This command has NOT been run against the owner's live account.** If remote revocation could not be confirmed, the CLI explicitly advises disconnecting the app in ChatGPT Settings. Never invoke it automatically.

**Limits:** The owner verified one completed streamed GPT answer and the separate staged GPT native tool + Claude Code acceptance (12/12 PASS on 2026-11-17). These short tests do not establish continuous background reliability, permitted unattended Claude Pro use or live OAuth refresh/revocation. Signing in itself does not start a paid OpenAI API call.



## Optional GPT checks in Codespaces (phase 1.6; owner live testing deferred)

This repository retains optional VS Code Tasks for when an owner-assisted live
acceptance is genuinely needed. **Do not try to run these on an iPhone now:**
the owner found Safari/VS Code Tasks unreliable, so all intermediate work uses
automated offline GitHub Actions. The combined 12/12 live function-call and
Claude acceptance passed once on 2026-11-17; a real refresh still awaits
separate owner authorization. For a later optional owner-assisted session:

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
   Windows interprocess locking passed native GitHub Actions tests, but a real
   Windows OAuth refresh remains unverified. This is still an owner-controlled
   pilot, not production storage.

The tasks do not run on workspace open. They keep OAuth credentials in the
Codespace user's protected storage; no secrets are passed as task arguments
or uploaded to GitHub. Task 1 (`auth status`) is read-only; tasks 2–4 check and
refresh the token near expiry as needed, without automatic model retries if
refresh fails. This has **offline test coverage only**. Live verification of
refresh/revocation and secure VM migration remain phase 1.7.
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

## Combined GPT + Claude Code acceptance (phase 1.6; live PASS 2026-11-17)

One owner-operated pilot replaces repeated phone-based VS Code Tasks. It has no
auto-run, no scheduled job and no production integration. A single VS Code task
`AI Agent Engine: 5. Combined GPT + Claude acceptance (LIVE)` invokes the
existing combined `run-live` command **only when the owner manually selects
it**. Tasks 1–4 remain for diagnostics but are no longer sequential prerequisites.
The owner does not need to retry those iPhone tasks now. The one-click task
still depends on the remote VS Code/Codespaces task picker working; this code
cannot repair mobile browser/task-palette synchronization.

Only later, on a host where the owner has already signed in to **both** personal accounts:

```bash
python3 -m ai_agent_engine.acceptance check
python3 -m ai_agent_engine.acceptance run-live
```

- `check` is read-only and makes **zero model requests**. It checks the local
  ChatGPT plan grant and the official `claude auth status` JSON; only a
  `claude.ai` authentication method is accepted. It does not refresh tokens.
  Any missing CLI, sign-in or alternate API/third-party authentication blocks.
- `run-live` is **explicitly opt-in** and runs one pass of 12 stages, each
  printed as `PASS`, `FAIL` or `SKIPPED` with a redacted reason:
  GPT sign-in → model available → real text reply → `write_new_file` call →
  file verified on disk → `read_file` call → read content verified → agent
  loop completed; Claude sign-in → personal Pro/Max subscription (first-party,
  no `ANTHROPIC_*`) → one real `claude -p` request → completed reply.
  GPT and Claude are independent: a GPT failure does not block Claude, and a
  failed step marks its dependants `SKIPPED`. Model usage is bounded: at most
  1 GPT text request + 4 GPT tool-cycle requests and 1 Claude request.
  No retries, no paid-API fallback. Exit code 0 only when every stage passes.
  `--report-json PATH` additionally saves the redacted stage list.
- The Claude probe does **not** use `--bare`: per the official CLI help,
  `--bare` never reads OAuth and accepts only `ANTHROPIC_API_KEY`, so it cannot
  run on a Claude Pro sign-in. It uses `--tools ""`, `--strict-mcp-config`,
  `--permission-mode plan`, `--no-session-persistence` and a temporary
  directory. Inherited `CLAUDE_CODE_*` session variables are not passed on.
  **Billing caveat:** `claude auth status` establishes the login method, but
  does **not** prove that Claude Extra Usage is disabled. Check Claude billing
  settings before opting into the live test.
- API/proxy-related `ANTHROPIC_*` and `CLAUDE_CODE_*` authentication-routing
  environment overrides are rejected. The CLI stdout/stderr on errors is
  never dumped to logs; only a bounded, successful, expected reply counts.
  **Do not run this via public CI or with an API key.**
- **Live result 2026-11-17** (Codespace, Claude Code 2.1.326, Python 3.12):
  all 12 stages PASS; GPT tool cycle used 3 model requests. Quota exhaustion,
  token refresh after expiry and revocation remain untested live.
- This is **a personal evaluation harness, not a deployed Claude backend**.
  Anthropic's [legal guidance](https://code.claude.com/docs/en/legal-and-compliance)
  distinguishes ordinary individual sign-in to unmodified Claude Code from
  embedding Claude in a product/service; commercial terms and API-key
  authentication may apply to the latter. Do not infer permission for
  unattended third-party subscription routing from this CLI test.
  Command flags: [official CLI reference](https://code.claude.com/docs/en/cli-reference).

## Development without the home PC (phase 1.5 preparation)

Source of truth is GitHub `vl-ktk/ai-agent-engine`, branch `main`.

- GitHub Actions [CI (offline)](.github/workflows/ci.yml) checks Python 3.11, 3.12 and 3.13 on Linux plus Python 3.12 on Windows on every push or pull request to `main`: compile, indexed-source safety scan, unit tests, local demo, installation of the core package, and an installed-package demo outside the checkout. No account credentials, AI requests, or deployment steps are present.
- Optional [Codespaces dev container](.devcontainer/devcontainer.json) supplies a remote Python 3.12 development environment. An owner-approved free-quota Codespace was **created and tested on 2026-11-16**: Python 3.12.11, 21/21 offline tests PASS. A password-protected noVNC desktop was also installed in that instance, reachable through one GitHub **Private** forwarded port; the real ChatGPT OAuth login and consent have since **passed** in this same-host desktop, and a live GPT model response passed. Native tool calls are still unverified. The GUI system packages were installed interactively and are not yet reproducible after container rebuild. GitHub Codespaces consumes personal account compute/storage quota, so stop the instance when idle. Details: [HANDOFF.md](./HANDOFF.md).
- These automated tests verify logic but cannot complete interactive OAuth, confirm user scopes or prove that a live GPT model answers.

## Project roadmap and architecture

- [Single implementation roadmap — 11 phases and their direct substeps](./IMPLEMENTATION_ROADMAP.md). Historical technical checklists are evidence only, not additional active task numbering.
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
