"""Provision inert private 7.7 files for the first and only approved scope.

This owner-admin entrypoint creates no owner decision, grant, task, service,
socket, archive collector or deployment. It refuses a pre-existing target and
preserves a failed new tree in the private job evidence folder for recovery.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess
import sys


ROOT = Path("/var/lib/ai-agent-engine-diagnostics")
WORKSPACE = ROOT / "workspaces" / "ai-agent-engine"
GITHUB_SOURCE = "https://github.com/vl-ktk/ai-agent-engine.git"
SERVICE_NAMES = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer")


def _git(workspace: Path, *arguments: str, timeout: int = 120) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_LFS_SKIP_SMUDGE": "1"}
    result = subprocess.run(
        ("/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c",
         "core.fsmonitor=false", "-c", "submodule.recurse=false", *arguments),
        cwd=workspace, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout, check=True,
    )
    if len(result.stdout) > 4096:
        raise RuntimeError("unexpected Git output")
    return result.stdout.strip()


def _git_as_owner(workspace: Path, uid: int, *arguments: str) -> str:
    if uid == os.geteuid():
        return _git(workspace, *arguments)
    username = pwd.getpwuid(uid).pw_name
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_LFS_SKIP_SMUDGE": "1"}
    result = subprocess.run(("/usr/sbin/runuser", "-u", username, "--",
                             "/usr/bin/git", "-C", str(workspace),
                             "-c", "core.hooksPath=/dev/null", "-c",
                             "core.fsmonitor=false", *arguments),
                            env=env, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=True)
    if len(result.stdout) > 4096:
        raise RuntimeError("unexpected owner Git output")
    return result.stdout.strip()


def _private_tree_owner(root: Path, uid: int, gid: int) -> None:
    """Change ownership only in a brand-new private tree; reject all links."""
    for parent, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in (*files, *directories):
            path = Path(parent) / name
            meta = path.lstat()
            if stat.S_ISLNK(meta.st_mode):
                raise RuntimeError("unexpected symlink in private source")
            if stat.S_ISDIR(meta.st_mode):
                mode = 0o700
            elif stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1:
                mode = 0o700 if meta.st_mode & 0o111 else 0o600
            else:
                raise RuntimeError("unexpected inode in private source")
            path.chmod(mode)
            os.chown(path, uid, gid)
    root.chmod(0o700)
    os.chown(root, uid, gid)


def _write_config(root: Path) -> None:
    doc = {
        "schema": 1,
        "owner_id": "vl-ktk",
        "project_ids": ["ai-agent-engine"],
        "project_leader_root": str(root / "projects"),
        "workspace_root": str(root / "workspaces"),
        "task_db": str(root / "tasks.sqlite3"),
        "socket_path": "/run/ai-agent-engine-diagnostics/intake.sock",
        "source_bindings": {"ai-agent-engine": str(root / "workspaces" / "ai-agent-engine")},
    }
    content = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    target = root / "runtime.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_schema(path: Path) -> tuple[int, int]:
    uri = "file:" + str(path) + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as database:
        return (database.execute("PRAGMA application_id").fetchone()[0],
                database.execute("PRAGMA user_version").fetchone()[0])


def _verify_state(root: Path, commit: str, uid: int) -> dict[str, object]:
    from ai_agent_engine.project_leader import REGISTRY_APPLICATION_ID, SCHEMA_VERSION as REGISTRY_VERSION
    from ai_agent_engine.task_storage import APPLICATION_ID as TASK_APPLICATION_ID, SCHEMA_VERSION as TASK_VERSION

    for directory in (root, root / "projects", root / "workspaces", root / "workspaces" / "ai-agent-engine"):
        meta = directory.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid
                or stat.S_IMODE(meta.st_mode) != 0o700):
            raise RuntimeError("unsafe private directory")
    for file in (root / "runtime.json", root / "tasks.sqlite3", root / "projects" / "project-leaders.sqlite3"):
        meta = file.lstat()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or meta.st_uid != uid or stat.S_IMODE(meta.st_mode) != 0o600):
            raise RuntimeError("unsafe private file")
    if _read_schema(root / "tasks.sqlite3") != (TASK_APPLICATION_ID, TASK_VERSION):
        raise RuntimeError("unexpected private task schema")
    if _read_schema(root / "projects" / "project-leaders.sqlite3") != (
            REGISTRY_APPLICATION_ID, REGISTRY_VERSION):
        raise RuntimeError("unexpected private registry schema")
    with sqlite3.connect("file:" + str(root / "projects" / "project-leaders.sqlite3")
                         + "?mode=ro&immutable=1", uri=True) as db:
        if db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] != 0:
            raise RuntimeError("unapproved registry must remain empty")
    if _git_as_owner(root / "workspaces" / "ai-agent-engine", uid, "rev-parse", "HEAD") != commit:
        raise RuntimeError("workspace commit mismatch")
    if _git_as_owner(root / "workspaces" / "ai-agent-engine", uid,
                     "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("workspace dirty")
    if not (root / "runtime.json").read_text(encoding="utf-8"):
        raise RuntimeError("empty private config")
    return {"scope": "vl-ktk/ai-agent-engine", "source_commit": commit,
            "task_schema": TASK_VERSION, "registry_schema": REGISTRY_VERSION,
            "owner_approval_created": False, "service_activated": False,
            "readiness": "HOLD_OWNER_APPROVAL_AND_RUNTIME_ACCEPTANCE"}


def provision_empty_project(root: Path, *, source_url: str, commit: str,
                            uid: int, gid: int, recovery: Path) -> dict[str, object]:
    """Only an absent, exact root may be populated; no existing data touched."""
    if (not root.is_absolute() or not recovery.is_absolute()
            or root.exists() or root.is_symlink() or len(commit) != 40
            or any(c not in "0123456789abcdef" for c in commit)):
        raise RuntimeError("unsafe or already provisioned target")
    if not recovery.is_dir() or recovery.is_symlink():
        raise RuntimeError("missing private recovery location")
    if root.parent.stat().st_dev != recovery.stat().st_dev:
        raise RuntimeError("private recovery must support atomic rename")
    created = False
    try:
        root.mkdir(mode=0o700)
        created = True
        (root / "projects").mkdir(mode=0o700)
        (root / "workspaces").mkdir(mode=0o700)

        # Initialize only brand-new private schemas; never migrate a foreign DB.
        from ai_agent_engine.project_leader import ProjectLeaderStore
        from ai_agent_engine.task_storage import SQLiteTaskStore
        with ProjectLeaderStore(root / "projects"):
            pass
        inbox_store = SQLiteTaskStore(root / "tasks.sqlite3")
        inbox_store.close()

        workspace = root / "workspaces" / "ai-agent-engine"
        workspace.mkdir(mode=0o700)
        _git(workspace, "init", "-q")
        _git(workspace, "remote", "add", "origin", source_url)
        _git(workspace, "fetch", "--no-tags", "--depth=1", "origin", commit)
        _git(workspace, "checkout", "--detach", "--quiet", commit)
        if _git(workspace, "rev-parse", "HEAD") != commit:
            raise RuntimeError("source not at selected GitHub commit")
        _write_config(root)
        _private_tree_owner(root, uid, gid)
        return _verify_state(root, commit, uid)
    except BaseException:
        if created and root.is_dir() and not root.is_symlink():
            # Preserve all newly created evidence; do not delete or clobber.
            target = recovery / "incomplete-single-project-provision"
            if not target.exists() and not target.is_symlink():
                root.rename(target)
        raise


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("owner-admin root channel required")
    os.umask(0o077)
    expected = os.environ["AE_EXPECTED_CURRENT"]
    if os.readlink("/opt/ai-agent-engine/current") != "releases/" + expected:
        raise RuntimeError("active release changed; refusing provisioning")
    for unit in SERVICE_NAMES:
        check = subprocess.run(("/usr/bin/systemctl", "is-active", "--quiet", unit),
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
        if check.returncode != 0:
            raise RuntimeError("baseline service not active")
    runtime = pwd.getpwnam("ae-runtime")
    evidence = Path(os.environ["AE_EVIDENCE"])
    if not evidence.is_dir() or evidence.is_symlink():
        raise RuntimeError("untrusted recovery directory")
    source = os.environ["AE_SOURCE_SHA"]
    sys.path.insert(0, str(Path.cwd()))  # Signed, source-pinned admin payload.
    report = provision_empty_project(ROOT, source_url=GITHUB_SOURCE,
                                     commit=source, uid=runtime.pw_uid,
                                     gid=runtime.pw_gid, recovery=evidence)
    # Detached, new data must remain private and empty until trusted owner gate.
    record = {"schema": 1, "status": "PREPARED_INERT", "release_unchanged": expected,
              **report}
    (evidence / "provision-7-7.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    print("PROVISION_7_7 " + json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
