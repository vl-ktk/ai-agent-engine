"""Stage 6.14: one scoped, disposable GPT/Claude E2E acceptance.

Offline uses deterministic *real* child processes. Live mode is opt-in and
uses only the existing personal subscription logins from 6.2 and 6.3. It
is never run by CI and never touches a working service or GitHub main.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .task_api import OwnerScope, TaskInbox, parse_submission
from .task_assignment import WorkspaceRoot
from .task_artifacts import TaskArtifactTransfer
from .task_claude import ClaudeTaskExecutor
from .task_gpt import GPTTaskExecutor
from .task_permissions import ActionGrant, ProtectedAction, TaskPermissionGate
from .task_response import ArtifactDescriptor
from .task_state import TaskState
from .task_storage import SQLiteTaskStore
from .task_versions import SourceVersionRejected, TaskSourceVersionRecorder


MARKERS = {"gpt": "AE-614-GPT", "claude": "AE-614-CLAUDE"}
PROJECT = "engine-614-isolated"
OWNER = OwnerScope("owner-614-private", frozenset({PROJECT}))

# Deterministic fixture accepts the *same* payload contract as actual workers.
# No provider request, internet access, secret or model call in CI.
FIXTURE_WORKER = (
    "import json,sys;"
    "request=json.loads(sys.stdin.buffer.read());"
    "print(json.dumps({'ok':True,'text':request['prompt']}),flush=True)"
)


class AcceptanceRejected(RuntimeError):
    """Sanitized: do not print model output or auth data."""


@dataclass(frozen=True, slots=True)
class ProviderCheck:
    provider: str
    task_id: str
    state: str
    base_commit: str
    result_commit: str
    rollback_verified: bool


def _git(repo: Path, *args: str) -> str:
    """A bounded local-only Git command, always in a new temporary repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
            check=False, text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AcceptanceRejected("local_git_unavailable") from None
    if result.returncode:
        raise AcceptanceRejected("local_git_failed")
    return result.stdout.strip()


def _make_disposable_git(repo: Path) -> str:
    _git(repo, "init", "-q")
    (repo / "fixture.txt").write_bytes(b"engine-614-fixture\n")
    _git(repo, "add", "--", "fixture.txt")
    _git(repo, "-c", "user.name=Offline Test",
         "-c", "user.email=test@example.invalid",
         "commit", "-qm", "private baseline")
    commit = _git(repo, "rev-parse", "--verify", "HEAD")
    if len(commit) not in (40, 64):
        raise AcceptanceRejected("invalid_git_baseline")
    return commit


def _test_rollback(recorder: TaskSourceVersionRecorder, task_id: str,
                   workspace: Path, baseline: str) -> None:
    """Version mismatch and reversal in a disposable, coordinator-owned repo.

    The probe commit is created by trusted TEST CODE, not by GPT or Claude.
    It only exists under TemporaryDirectory. No Git push/reset/deploy.
    """
    (workspace / "rollback-probe.txt").write_text("local test revision\n")
    _git(workspace, "add", "--", "rollback-probe.txt")
    _git(workspace, "-c", "user.name=Offline Test",
         "-c", "user.email=test@example.invalid",
         "commit", "-qm", "rollback detection probe")
    probe = _git(workspace, "rev-parse", "HEAD")
    if probe == baseline:
        raise AcceptanceRejected("rollback_probe_not_created")
    try:
        recorder.verify_version(task_id)
    except SourceVersionRejected as exc:
        if exc.code != "source_changed":
            raise AcceptanceRejected("unexpected_version_check") from None
    else:
        raise AcceptanceRejected("source_change_not_detected")
    # The probe commit remains reachable from its original local branch:
    # switching to baseline is a reversible test checkout, not git reset.
    _git(workspace, "switch", "--detach", baseline)
    if _git(workspace, "rev-parse", "HEAD") != baseline:
        raise AcceptanceRejected("rollback_failed")
    if recorder.verify_version(task_id).result_commit != baseline:
        raise AcceptanceRejected("rollback_not_verified")


def run_acceptance(*, live: bool = False, owner_approved: bool = False,
                   providers: tuple[str, ...] = ("gpt", "claude")) -> tuple[ProviderCheck, ...]:
    """Run through inbox, actual subprocess, grant, ACK, artifact, version, rollback.

    Live: one request to each named, already-authorized personal provider.
    Never retries or starts a hosted service. No paid API credential fallback.
    """
    if live and not owner_approved:
        raise AcceptanceRejected("owner_approval_required")
    if (not providers or len(providers) != len(set(providers))
            or any(p not in MARKERS for p in providers)):
        raise AcceptanceRejected("invalid_provider_selection")
    results: list[ProviderCheck] = []
    with tempfile.TemporaryDirectory(prefix="ae-614-private-") as dirname:
        root = Path(dirname)
        workspaces = root / "workspaces"
        workspaces.mkdir(mode=0o700)
        bases: dict[str, str] = {}
        for provider in providers:
            folder = workspaces / provider
            folder.mkdir(mode=0o700)
            bases[provider] = _make_disposable_git(folder)
        db_path = root / "task.sqlite3"
        inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                          workspace_root=WorkspaceRoot(workspaces))
        runners: list[GPTTaskExecutor] = []
        tasks: list[tuple[str, str, str]] = []
        try:
            for provider in providers:
                worker = provider + "-614"
                marker = MARKERS[provider]
                task_id = inbox.accept(parse_submission(json.dumps({
                    "project_id": PROJECT,
                    "title": "Owner-scoped isolated stage 6.14 acceptance",
                    "prompt": "Reply with only the marker " + marker + ".",
                }).encode()), OWNER).task_id
                grant = TaskPermissionGate(inbox, grants=(
                    ActionGrant(task_id, worker, ProtectedAction.EXTERNAL_REQUEST),))
                kwargs = {
                    "worker_id": worker, "model_id":
                        "gpt-6.1-sol" if provider == "gpt" else "default",
                    "permission_gate": grant,
                    "timeout_seconds": 90 if provider == "gpt" else 210,
                }
                if not live:
                    kwargs["worker_argv"] = (
                        sys.executable, "-u", "-c", FIXTURE_WORKER)
                runner = (
                    GPTTaskExecutor(inbox, OWNER, **kwargs)
                    if provider == "gpt" else
                    ClaudeTaskExecutor(inbox, OWNER,
                                       authorized_owner_id=OWNER.owner_id, **kwargs))
                runners.append(runner)
                outcome = runner.execute(task_id, provider)
                if (outcome.state is not TaskState.SUCCEEDED
                        or outcome.executed is not True
                        or outcome.text is None or marker not in outcome.text
                        or inbox.state(task_id, OWNER) is not TaskState.SUCCEEDED):
                    raise AcceptanceRejected("provider_task_not_succeeded")
                if ([e.allowed for e in grant.audit_history(task_id, OWNER)] != [True]
                        or [(x.provider, x.outcome)
                            for x in runner.provider_history(task_id)]
                        != [(provider, "completed")]
                        or runner.delivery.snapshot(task_id, OWNER).status != "completed"):
                    raise AcceptanceRejected("audit_or_delivery_incomplete")
                # Explicitly coordinator-owned test fixture; not a file
                # generated by the LLM or shared from another project.
                data = (workspaces / provider / "fixture.txt").read_bytes()
                descriptor = ArtifactDescriptor(
                    "fixture.txt", sha256(data).hexdigest(),
                    len(data), "text/plain")
                transfer = TaskArtifactTransfer(inbox, OWNER)
                bundle = transfer.ingest(runner, outcome, (descriptor,))
                if bundle.read_file("fixture.txt") != data:
                    raise AcceptanceRejected("artifact_transfer_failed")
                versioner = TaskSourceVersionRecorder(inbox, OWNER)
                receipt = versioner.record(
                    runner, outcome, baseline_commit=bases[provider])
                if (receipt.result_commit != bases[provider]
                        or receipt.patch_sha256 is not None
                        or versioner.verify_version(task_id) != receipt):
                    raise AcceptanceRejected("source_version_unverified")
                _test_rollback(versioner, task_id,
                               workspaces / provider, bases[provider])
                tasks.append((provider, task_id, bases[provider]))
                results.append(ProviderCheck(provider, task_id, "succeeded",
                                             bases[provider],
                                             receipt.result_commit, True))

            for runner in runners:
                runner.close()
            runners.clear()
            inbox.close()
            inbox = TaskInbox(store=SQLiteTaskStore(db_path),
                              workspace_root=WorkspaceRoot(workspaces))
            transfer = TaskArtifactTransfer(inbox, OWNER)
            versioner = TaskSourceVersionRecorder(inbox, OWNER)
            for provider, task_id, base in tasks:
                if (inbox.state(task_id, OWNER) is not TaskState.SUCCEEDED
                        or transfer.read_bundle(task_id).response.provider != provider
                        or transfer.read_bundle(task_id).read_file("fixture.txt")
                        != b"engine-614-fixture\n"
                        or versioner.verify_version(task_id).result_commit != base
                        or inbox._store.process_record(task_id)[-1] != "stopped"
                        or [(row[2], row[4]) for row in
                            inbox._store.provider_history(task_id)]
                        != [(provider, "completed")]):
                    raise AcceptanceRejected("durable_recovery_unverified")
            return tuple(results)
        finally:
            for runner in runners:
                runner.close()
            inbox.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Isolated stage 6.14 owner E2E acceptance")
    parser.add_argument("--live", action="store_true",
                        help="One real subscription call per provider, not for CI")
    parser.add_argument("--owner-approved", action="store_true",
                        help="Explicit opt-in required for live provider requests")
    parser.add_argument("--provider", choices=("gpt", "claude", "both"), default="both")
    args = parser.parse_args(argv)
    providers = ("gpt", "claude") if args.provider == "both" else (args.provider,)
    try:
        checks = run_acceptance(live=args.live,
                                owner_approved=args.owner_approved,
                                providers=providers)
    except Exception:
        # Never reveal provider replies, account data, private paths or secrets.
        print("STAGE6_14_" + ("LIVE" if args.live else "OFFLINE")
              + " BLOCKED: inspect private local tests; no claim of acceptance")
        return 2
    print("STAGE6_14_" + ("LIVE" if args.live else "OFFLINE")
          + " PASS: " + ",".join(r.provider for r in checks)
          + "; scoped queue/grants/SQLite artifact+source version/rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
