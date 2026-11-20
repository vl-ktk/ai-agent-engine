"""Owner-gated, one-project 7.7 registration; never activates a service.

The immutable GitHub owner-admin request is the authentication boundary:
the workflow accepts only a single owner-authored request commit pinning this
reviewed script and its SHA-256. This file must not be used as a standalone
approval interface or triggered by untrusted model output.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


ROOT = Path("/var/lib/ai-agent-engine-diagnostics")
PROPOSAL_FILE = "ops/owner-proposals/ai-agent-engine-7-7.json"
PROPOSAL_FILE_SHA256 = "2423890a811b7ac9f2b5b7f09479772cf38a1091bb1170fc5ee4142abf0d9650"
PREPARED_SOURCE_SHA = "741fc1a4e62b52922611e1e28d4805d727dffe4d"
OWNER = "vl-ktk"
PROJECT = "ai-agent-engine"
TITLE = "AI Agent Engine"
PROPOSAL_ID = "proposal_8950a54bb02d4537943c5ed6f3286696"
PROPOSAL_SHA = "8443ee2fd8de2cb0c893a39420b0229b463a055b014f3bf93c571952f573dd56"
SERVICE_NAMES = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate proposal field")
        result[key] = value
    return result


def load_pinned_proposal(source: Path):
    """Read precisely the source-reviewed proposal, without inferring consent."""
    from ai_agent_engine.project_leader import ProjectProposal, _digest

    path = source / PROPOSAL_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing pinned owner proposal")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != PROPOSAL_FILE_SHA256:
        raise ValueError("owner proposal content changed")
    doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    if (type(doc) is not dict or set(doc) != {
            "schema", "proposal_id", "owner_id", "project_id", "title", "sha256"}
            or type(doc["schema"]) is not int or doc["schema"] != 1
            or any(type(doc[k]) is not str for k in (
                "proposal_id", "owner_id", "project_id", "title", "sha256"))
            or not re.fullmatch(r"proposal_[0-9a-f]{32}", doc["proposal_id"])
            or doc["proposal_id"] != PROPOSAL_ID or doc["sha256"] != PROPOSAL_SHA
            or doc["owner_id"] != OWNER or doc["project_id"] != PROJECT
            or doc["title"] != TITLE
            or doc["sha256"] != _digest(OWNER, PROJECT, TITLE)):
        raise ValueError("invalid pinned owner proposal")
    return ProjectProposal(doc["proposal_id"], OWNER, PROJECT, TITLE, doc["sha256"])


def register_approved_project(root: Path, proposal, decision, *,
                              prepared_source_sha: str = PREPARED_SOURCE_SHA):
    """Apply an externally authenticated exact decision to already prepared data.

    Tests may supply a synthetic decision; only the GitHub owner-admin main()
    may supply one on a real host. No task, grant, socket or service is created.
    """
    from ai_agent_engine.diagnostic_host_preflight import (
        _private_directory, _readonly_sqlite,
    )
    from ai_agent_engine.diagnostic_intake_service import _private_config
    from ai_agent_engine.main_orchestrator import OwnerDecision
    from ai_agent_engine.project_diagnostics import capture_git_source
    from ai_agent_engine.project_leader import (
        ProjectLeaderStore, ProjectProposal, PROJECT_SCHEMA_VERSION,
    )
    from ai_agent_engine.task_api import OwnerScope
    from ai_agent_engine.task_storage import (
        APPLICATION_ID as TASK_APP, SCHEMA_VERSION as TASK_VERSION,
    )

    root = Path(root)
    if (not root.is_absolute() or root.is_symlink()
            or not isinstance(proposal, ProjectProposal)
            or not isinstance(decision, OwnerDecision)
            or proposal.owner_id != OWNER or proposal.project_id != PROJECT
            or proposal.title != TITLE
            or proposal.proposal_id != PROPOSAL_ID or proposal.sha256 != PROPOSAL_SHA
            or decision.owner_id != OWNER
            or decision.proposal_id != proposal.proposal_id
            or decision.proposal_sha256 != proposal.sha256
            or decision.channel != "button"
            or not re.fullmatch(r"[0-9a-f]{40}", prepared_source_sha)):
        raise ValueError("owner registration requires exact approved proposal")

    _private_directory(root)
    config = _private_config(root / "runtime.json")
    expected = {
        "owner_id": OWNER, "project_ids": [PROJECT],
        "project_leader_root": str(root / "projects"),
        "workspace_root": str(root / "workspaces"),
        "task_db": str(root / "tasks.sqlite3"),
        "socket_path": "/run/ai-agent-engine-diagnostics/intake.sock",
        "source_bindings": {PROJECT: str(root / "workspaces" / PROJECT)},
    }
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError("unexpected single-project host configuration")
    projects = root / "projects"
    workspace = root / "workspaces" / PROJECT
    for directory in (projects, root / "workspaces", workspace):
        _private_directory(directory)
    with _readonly_sqlite(root / "tasks.sqlite3", TASK_APP, TASK_VERSION) as task_db:
        if task_db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] != 0:
            raise ValueError("first-project task database is not empty")
    version = capture_git_source(workspace, prepared_source_sha)
    if version.patch_sha256 is not None:
        raise ValueError("prepared Git checkout has changed")

    scope = OwnerScope(OWNER, frozenset({PROJECT}))
    with ProjectLeaderStore(projects) as store:
        rows = store._db().execute(
            "SELECT owner_id,project_id,proposal_id,proposal_sha256,approval_channel "
            "FROM projects").fetchall()
        if rows and rows != [(
                OWNER, PROJECT, proposal.proposal_id, proposal.sha256, "button")]:
            raise ValueError("project registry contains unexpected approvals")
        created = not rows
        result = store.approve(scope, proposal, decision)
        if store.get(scope, PROJECT) != result:
            raise RuntimeError("approved project could not be reopened")
    import sqlite3
    with sqlite3.connect(result.database.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != PROJECT_SCHEMA_VERSION:
            raise RuntimeError("project leader schema not ready")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("project leader database not healthy")
    return {"status": "APPROVED_INERT", "scope": OWNER + "/" + PROJECT,
            "proposal_id": proposal.proposal_id, "proposal_sha256": proposal.sha256,
            "source_commit": prepared_source_sha, "project_schema": PROJECT_SCHEMA_VERSION,
            "approval_record_created": created, "service_activated": False}


@contextmanager
def _as_runtime_user(uid: int, gid: int):
    """Make all new private database files belong to ae-runtime, never root."""
    old_uid, old_gid = os.geteuid(), os.getegid()
    os.setegid(gid)
    os.seteuid(uid)
    try:
        yield
    finally:
        os.seteuid(old_uid)
        os.setegid(old_gid)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("GitHub owner-admin root channel required")
    os.umask(0o077)
    for field in ("AE_REQUEST_SHA", "AE_SOURCE_SHA", "AE_EXPECTED_CURRENT"):
        if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get(field, "")):
            raise RuntimeError("missing immutable owner-admin request")
    if os.readlink("/opt/ai-agent-engine/current") != (
            "releases/" + os.environ["AE_EXPECTED_CURRENT"]):
        raise RuntimeError("active release changed")
    for service in SERVICE_NAMES:
        result = subprocess.run(
            ("/usr/bin/systemctl", "is-active", "--quiet", service),
            check=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
        if result.returncode:
            raise RuntimeError("baseline service is not active")

    # Verified parent SHA, authenticated github.actor, script digest and the
    # one-file owner request are enforced by netcup-owner-admin.yml and its
    # request validator BEFORE this code runs on Netcup.
    evidence = Path(os.environ["AE_EVIDENCE"])
    if not evidence.is_dir() or evidence.is_symlink():
        raise RuntimeError("missing private admin evidence folder")
    sys.path.insert(0, str(Path.cwd()))
    proposal = load_pinned_proposal(Path.cwd())
    # The immutable admin source is root-private. Import the entire trusted
    # 7.7 registration dependency set BEFORE reducing effective UID.
    from ai_agent_engine import (
        diagnostic_host_preflight, diagnostic_intake_service,
        project_diagnostics, project_leader, task_api, task_storage,
    )
    from ai_agent_engine.main_orchestrator import OwnerDecision
    decision = OwnerDecision(OWNER, proposal.proposal_id, proposal.sha256, "button")
    runtime = pwd.getpwnam("ae-runtime")
    with _as_runtime_user(runtime.pw_uid, runtime.pw_gid):
        report = register_approved_project(ROOT, proposal, decision)
    (evidence / "approved-project-7-7.json").write_text(
        json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8")
    print("OWNER_PROJECT_7_7 " + json.dumps(
        report, separators=(",", ":"), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
