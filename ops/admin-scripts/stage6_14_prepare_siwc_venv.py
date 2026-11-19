"""Prepare a separate owner-only GPT SIWC login Python environment on Netcup.

Do not mutate the immutable production release or user credentials. Install
only the declared free SIWC Python extras into a new isolated venv and copy
the already-deployed ai_agent_engine package code without changing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sysconfig


ROOT = Path("/opt/ai-agent-engine")
TARGET = ROOT / "owner-siwc-6-14"
USER = "ae-runtime"
RELEASE = os.environ.get("AE_EXPECTED_CURRENT", "")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def run(argv: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=False, umask=0o022,
    )


def baseline() -> None:
    if os.geteuid() != 0 or not SHA.fullmatch(RELEASE):
        raise SystemExit("requires pinned owner admin release")
    if os.readlink(ROOT / "current") != "releases/" + RELEASE:
        raise SystemExit("production release changed")
    if run(["systemctl", "is-active", "--quiet", "ae-controller.service",
            "ae-backup.timer", "ae-host-check.timer"], timeout=15).returncode:
        raise SystemExit("production health check failed")


def purelib(python: Path) -> Path:
    cmd = run([str(python), "-I", "-c",
               "import sysconfig; print(sysconfig.get_path('purelib'))"], timeout=15)
    if cmd.returncode:
        raise SystemExit("Python library directory unavailable")
    value = cmd.stdout.decode("ascii", errors="strict").strip()
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.name != "site-packages":
        raise SystemExit("invalid Python library directory")
    return path


def as_runtime(argv: list[str], *, timeout: int = 30) -> bool:
    home = pwd.getpwnam(USER).pw_dir
    env = ["/usr/bin/env", "-i", "HOME=" + home,
           "USER=" + USER, "LOGNAME=" + USER, "LANG=C.UTF-8",
           "PATH=" + home + "/.local/bin:/usr/local/bin:/usr/bin:/bin"]
    return run(["/usr/sbin/runuser", "-u", USER, "--", *env, *argv],
               timeout=timeout).returncode == 0


def ensure_owner_venv() -> tuple[bool, Path]:
    destination = TARGET / "venv"
    marker = TARGET / "setup-complete.json"
    if TARGET.exists() or TARGET.is_symlink():
        if TARGET.is_symlink() or not TARGET.is_dir() or not marker.is_file():
            raise SystemExit("incomplete or unexpected owner SIWC directory; manual review needed")
        return False, destination / "bin/python"
    source_lib = purelib(ROOT / "current/venv/bin/python")
    source_package = source_lib / "ai_agent_engine"
    if source_package.is_symlink() or not source_package.is_dir():
        raise SystemExit("deployed ai_agent_engine package missing")
    # Isolated owner helper; no modification to current release or running units.
    TARGET.mkdir(mode=0o755)
    TARGET.chmod(0o755)
    created = run(["/usr/bin/python3", "-m", "venv", str(destination)], timeout=60)
    if created.returncode:
        raise SystemExit("owner SIWC venv creation failed; output withheld")
    python = destination / "bin/python"
    # Exact version range is taken from pyproject.toml [project.optional-dependencies].
    installed = run([str(python), "-I", "-m", "pip", "install",
                     "--disable-pip-version-check", "--no-input",
                     "--only-binary=:all:", "PyJWT[crypto]>=2.10,<3"], timeout=240)
    if installed.returncode:
        raise SystemExit("SIWC optional dependencies installation failed; output withheld")
    dest_lib = purelib(python)
    package_dest = dest_lib / "ai_agent_engine"
    if package_dest.exists() or package_dest.is_symlink():
        raise SystemExit("unmanaged ai_agent_engine copy exists; refused overwrite")
    shutil.copytree(source_package, package_dest, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Metadata lets importlib.metadata resolve the same deployed package version.
    metadata = list(source_lib.glob("ai_agent_engine-*.dist-info"))
    if len(metadata) != 1 or metadata[0].is_symlink():
        raise SystemExit("deployed package metadata ambiguous")
    shutil.copytree(metadata[0], dest_lib / metadata[0].name,
                    symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))
    for directory, _, files in os.walk(TARGET, followlinks=False):
        os.chmod(directory, 0o755)
        for name in files:
            entry = Path(directory) / name
            if not entry.is_symlink():
                os.chmod(entry, 0o755 if entry.stat().st_mode & 0o111 else 0o644)
    if not as_runtime([str(python), "-I", "-c",
                       "import ai_agent_engine, jwt, cryptography; "
                       "from ai_agent_engine import auth, siwc"], timeout=30):
        raise SystemExit("SIWC modules cannot load under ae-runtime")
    if not as_runtime([str(python), "-I", "-m", "ai_agent_engine.auth", "status"]):
        raise SystemExit("owner SIWC status command failed")
    marker.write_text(json.dumps({"source_release": RELEASE,
                                  "python": "venv/bin/python",
                                  "status": "ready_to_sign_in"}, sort_keys=True) + "\n")
    marker.chmod(0o644)
    return True, python


def main() -> None:
    baseline()
    created, python = ensure_owner_venv()
    if not python.is_file() or not as_runtime([str(python), "-I", "-c",
        "import ai_agent_engine, jwt, cryptography; from ai_agent_engine import siwc"]):
        raise SystemExit("owner SIWC venv verification failed")
    baseline()
    print("STAGE6_14_SIWC_PREP " + json.dumps({
        "status": "GPT_CLI_READY_FOR_OWNER_LOGIN", "created_now": created,
        "python_dependencies_ready": True, "owner_account": USER,
        "models_invoked": False, "auth_attempted": False,
        "credentials_accessed": False, "release_unchanged": True,
        "services_healthy": True, "production_changes": False,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
