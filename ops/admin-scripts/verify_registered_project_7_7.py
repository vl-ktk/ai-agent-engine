"""Independent read-only verification of the first approved Netcup project.

This command does not grant approval, write application data, launch a task,
or change systemd. Owner-admin authentication and pinned-release enforcement
are provided by the reviewed GitHub workflow before this code is invoked.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import subprocess


ROOT = Path("/var/lib/ai-agent-engine-diagnostics")
PREPARED_SOURCE_SHA = "741fc1a4e62b52922611e1e28d4805d727dffe4d"
LIVE_RELEASE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
OWNER = "vl-ktk"
PROJECT = "ai-agent-engine"
TITLE = "AI Agent Engine"
PROPOSAL_ID = "proposal_8950a54bb02d4537943c5ed6f3286696"
PROPOSAL_SHA = "8443ee2fd8de2cb0c893a39420b0229b463a055b014f3bf93c571952f573dd56"
SERVICES = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def verify_registered_project(root: Path, *, prepared_source_sha: str = PREPARED_SOURCE_SHA):
    """Open all databases immutably and check the exact persisted approval."""
    from ai_agent_engine.diagnostic_host_preflight import (
        _private_directory, _readonly_sqlite, inspect_preprovisioned_host,
    )
    from ai_agent_engine.diagnostic_intake_service import _private_config
    from ai_agent_engine.project_leader import (
        PROJECT_APPLICATION_ID, PROJECT_SCHEMA_VERSION,
        REGISTRY_APPLICATION_ID, SCHEMA_VERSION as REGISTRY_SCHEMA_VERSION,
    )
    from ai_agent_engine.task_storage import (
        APPLICATION_ID as TASK_APPLICATION_ID, SCHEMA_VERSION as TASK_SCHEMA_VERSION,
    )

    root = Path(root)
    _private_directory(root)
    config = _private_config(root / "runtime.json")
    if (config.get("owner_id") != OWNER or config.get("project_ids") != [PROJECT]
            or config.get("project_leader_root") != str(root / "projects")
            or config.get("task_db") != str(root / "tasks.sqlite3")
            or config.get("source_bindings") != {
                PROJECT: str(root / "workspaces" / PROJECT)}):
        raise ValueError("unexpected first-project private configuration")
    socket = Path(config["socket_path"])
    if socket.exists() or socket.is_symlink():
        raise ValueError("diagnostic socket unexpectedly exists")

    readiness = inspect_preprovisioned_host(root / "runtime.json")
    required = {"private_config", "existing_project_approval",
                "isolated_inactive_task_db", "clean_verified_git_source"}
    if (not required.issubset(readiness.checked)
            or set(readiness.blockers) - {"private_host_directories"}
            or readiness.source_commit != prepared_source_sha):
        raise ValueError("approved project preflight did not pass required checks")

    expected = (OWNER, PROJECT, TITLE, PROPOSAL_ID, PROPOSAL_SHA, "button", "active")
    projects = root / "projects"
    with _readonly_sqlite(projects / "project-leaders.sqlite3",
                          REGISTRY_APPLICATION_ID, REGISTRY_SCHEMA_VERSION) as db:
        rows = db.execute("SELECT owner_id,project_id,title,proposal_id,"
                          "proposal_sha256,approval_channel,state FROM projects").fetchall()
        if rows != [expected]:
            raise ValueError("unexpected or extra project approvals")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("project registry integrity failed")
    with _readonly_sqlite(projects / OWNER / PROJECT / "leader.sqlite3",
                          PROJECT_APPLICATION_ID, PROJECT_SCHEMA_VERSION) as db:
        identities = db.execute("SELECT owner_id,project_id,title,proposal_id,"
                                "proposal_sha256,approval_channel FROM identity").fetchall()
        if identities != [expected[:6]]:
            raise ValueError("project leader identity mismatch")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("project leader integrity failed")
    with _readonly_sqlite(root / "tasks.sqlite3",
                          TASK_APPLICATION_ID, TASK_SCHEMA_VERSION) as db:
        if db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
            raise ValueError("diagnostic tasks unexpectedly exist")
    return {"status": "APPROVAL_VERIFIED_READ_ONLY", "scope": OWNER + "/" + PROJECT,
            "proposal_id": PROPOSAL_ID, "proposal_sha256": PROPOSAL_SHA,
            "prepared_source": prepared_source_sha, "project_schema": PROJECT_SCHEMA_VERSION,
            "open_preflight_blockers": readiness.blockers, "service_activated": False}


@contextmanager
def _as_runtime_user(uid: int, gid: int):
    old_uid, old_gid = os.geteuid(), os.getegid()
    os.setegid(gid)
    os.seteuid(uid)
    try:
        yield
    finally:
        os.seteuid(old_uid)
        os.setegid(old_gid)


def _baseline_healthy(expected_release: str) -> None:
    if os.readlink("/opt/ai-agent-engine/current") != "releases/" + expected_release:
        raise RuntimeError("active server release has changed")
    for service in SERVICES:
        result = subprocess.run(
            ("/usr/bin/systemctl", "is-active", "--quiet", service),
            capture_output=True, stdin=subprocess.DEVNULL, check=False, timeout=8)
        if result.returncode:
            raise RuntimeError("baseline service changed: " + service)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("authenticated GitHub owner-admin root channel required")
    expected = os.environ.get("AE_EXPECTED_CURRENT", "")
    if expected != LIVE_RELEASE_SHA or not os.environ.get("AE_REQUEST_SHA"):
        raise RuntimeError("unexpected immutable owner-admin request")
    _baseline_healthy(expected)
    # All project imports are loaded before lowering euid: the admin source
    # is root-private, whereas the target files belong only to ae-runtime.
    from ai_agent_engine import (
        diagnostic_host_preflight, diagnostic_intake_service,
        project_diagnostics, project_leader, task_storage,
    )
    runtime = pwd.getpwnam("ae-runtime")
    with _as_runtime_user(runtime.pw_uid, runtime.pw_gid):
        report = verify_registered_project(ROOT)
    _baseline_healthy(expected)
    evidence = Path(os.environ["AE_EVIDENCE"])
    if evidence.is_symlink() or not evidence.is_dir():
        raise RuntimeError("missing private admin evidence")
    (evidence / "verified-approved-project-7-7.json").write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")
    print("VERIFY_PROJECT_7_7 " + json.dumps(
        report, sort_keys=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
