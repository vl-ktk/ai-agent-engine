"""Owner-approved 7.7 host inventory, with NO application mutations.

The existing root-admin transport writes its own private, immutable evidence
directory. This selected script reads fixed OS/runtime locations only and
records a bounded, data-free report in that already-created evidence folder.
It neither provisions a project nor deploys, installs or restarts anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import time


CURRENT = Path("/opt/ai-agent-engine/current")
DATA = Path("/var/lib/ai-agent-engine")
DIAG = Path("/var/lib/ai-agent-engine-diagnostics")
CONFIG = Path("/etc/ai-agent-engine/diagnostic-runtime.json")
SERVICE_NAMES = (
    "ae-controller.service",
    "ae-backup.timer",
    "ae-host-check.timer",
)


def _fixed_file_status(path: Path, *, directory: bool, owner_uid: int) -> str:
    """Return a bounded marker: do not follow symlinks or reveal file data."""
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    if stat.S_ISLNK(meta.st_mode):
        return "symlink_refused"
    if directory and not stat.S_ISDIR(meta.st_mode):
        return "wrong_type"
    if not directory and not stat.S_ISREG(meta.st_mode):
        return "wrong_type"
    if meta.st_uid != owner_uid:
        return "unexpected_owner"
    if stat.S_IMODE(meta.st_mode) != (0o700 if directory else 0o600):
        return "unsafe_permissions"
    return "private_existing"


def _cpu_counters(line: str) -> tuple[int, int]:
    fields = line.split()
    if len(fields) < 9 or fields[0] != "cpu":
        raise ValueError("invalid aggregate CPU counters")
    # Guest ticks are already part of user/nice time in /proc/stat. Excluding
    # the final two optional guest fields avoids counting them twice.
    counters = tuple(int(value) for value in fields[1:9])
    if any(value < 0 for value in counters):
        raise ValueError("invalid CPU counter")
    idle = counters[3] + counters[4]
    return sum(counters), idle


def _cpu_busy(first: str, second: str) -> float:
    total0, idle0 = _cpu_counters(first)
    total1, idle1 = _cpu_counters(second)
    total_delta = total1 - total0
    idle_delta = idle1 - idle0
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        raise ValueError("invalid CPU sampling window")
    return round(100.0 * (total_delta - idle_delta) / total_delta, 1)


def _host_cpu() -> float:
    with open("/proc/stat", encoding="ascii") as source:
        before = source.readline()
    time.sleep(0.35)
    with open("/proc/stat", encoding="ascii") as source:
        after = source.readline()
    return _cpu_busy(before, after)


def _host_memory() -> float:
    wanted = {}
    with open("/proc/meminfo", encoding="ascii") as source:
        for line in source:
            key, sep, value = line.partition(":")
            if sep and key in ("MemTotal", "MemAvailable"):
                wanted[key] = int(value.strip().split()[0])
    total, available = wanted["MemTotal"], wanted["MemAvailable"]
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("invalid memory counters")
    return round((total - available) * 100.0 / total, 1)


def _host_disk() -> float:
    info = os.statvfs(DATA)
    if info.f_blocks <= 0 or not 0 <= info.f_bfree <= info.f_blocks:
        raise ValueError("invalid disk counters")
    return round(100.0 * (info.f_blocks - info.f_bfree) / info.f_blocks, 1)


def _systemctl_active(unit: str) -> bool:
    checked = subprocess.run(
        ("/usr/bin/systemctl", "is-active", "--quiet", unit),
        stdin=subprocess.DEVNULL, capture_output=True, timeout=8, check=False,
    )
    return checked.returncode == 0


def _installed_version() -> str:
    target = "/opt/ai-agent-engine/current/venv/bin/python"
    completed = subprocess.run(
        (target, "-I", "-c", "import importlib.metadata as m; print(m.version('ai-agent-engine'))"),
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=10, check=True,
    )
    value = completed.stdout.strip()
    if not value.startswith("0.3.") or len(value) > 20:
        raise ValueError("unexpected package version")
    return value


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("root channel required for metadata-only host audit")
    expected = os.environ["AE_EXPECTED_CURRENT"]
    if os.readlink(CURRENT) != "releases/" + expected:
        raise RuntimeError("installed release changed; HOLD")
    uid = pwd.getpwnam("ae-runtime").pw_uid
    services = {unit: _systemctl_active(unit) for unit in SERVICE_NAMES}
    if not all(services.values()):
        raise RuntimeError("baseline service stopped; HOLD")

    # These paths are proposed in source; absence is not an instruction to
    # create them. A differently located config still requires owner review.
    markers = {
        "proposed_diagnostic_root": _fixed_file_status(DIAG, directory=True, owner_uid=uid),
        "proposed_project_root": _fixed_file_status(DIAG / "projects", directory=True, owner_uid=uid),
        "proposed_workspace_root": _fixed_file_status(DIAG / "workspaces", directory=True, owner_uid=uid),
        "proposed_task_db": _fixed_file_status(DIAG / "tasks.sqlite3", directory=False, owner_uid=uid),
        "proposed_project_registry": _fixed_file_status(
            DIAG / "projects" / "project-leaders.sqlite3", directory=False, owner_uid=uid),
        "proposed_runtime_config": _fixed_file_status(CONFIG, directory=False, owner_uid=uid),
    }
    # A present file is never enough to prove an authenticated approval,
    # correct schema/grants, or a running diagnostic worker.
    report = {
        "schema": 1,
        "deployed_sha": expected,
        "installed_version": _installed_version(),
        "baseline_units_active": all(services.values()),
        "candidate_markers": markers,
        "host_resource_snapshot_pct": {
            "cpu_busy_short_sample": _host_cpu(),
            "ram_used": _host_memory(),
            "disk_used": _host_disk(),
        },
        "resource_numbers_are_host_wide_not_7_7_overhead": True,
        "diagnostic_runtime_activated": "not_proven_by_this_audit",
        "readiness": "HOLD_OWNER_PROVISIONING_AND_LIVE_QA",
    }
    evidence = Path(os.environ["AE_EVIDENCE"])
    if evidence.is_symlink() or not evidence.is_dir():
        raise RuntimeError("unsafe evidence directory")
    (evidence / "host-audit-7-7.json").write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print("HOST_AUDIT_7_7 " + json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
