"""Read-only owner-gated Netcup 7.7 diagnostic resource measurement.

Only fixed systemd cgroup counters and aggregate filesystem byte counts are
read. No service state, private file content, tasks, grants, or backup is
modified. A bounded, data-free JSON proof is stored in admin evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time


CORE_SHA = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
CURRENT = Path("/opt/ai-agent-engine/current")
CGROUP = Path("/sys/fs/cgroup/system.slice/ae-managed-diagnostics.service")
PRIVATE = Path("/var/lib/ai-agent-engine-diagnostics")
V1 = Path("/opt/ai-agent-engine/diagnostic-7-7")
V2 = Path("/opt/ai-agent-engine/diagnostic-7-7-v2")
BACKUPS = Path("/var/backups/ai-agent-engine-diagnostics")
UNITS = ("ae-controller.service", "ae-backup.timer", "ae-host-check.timer",
         "ae-managed-diagnostics.service")


def _run(*args: str) -> str:
    result = subprocess.run(args, check=True, timeout=12, text=True,
                            stdin=subprocess.DEVNULL, capture_output=True)
    return result.stdout.strip()


def _counter(path: Path) -> int:
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9]{1,30}", value):
        raise RuntimeError("unexpected cgroup counter format")
    return int(value)


def _cpu_usage_usec(path: Path) -> int:
    entries = path.read_text(encoding="ascii").splitlines()
    matches = [line.split()[1] for line in entries
               if re.fullmatch(r"usage_usec [0-9]{1,30}", line)]
    if len(matches) != 1:
        raise RuntimeError("missing or duplicate cgroup cpu usage")
    return int(matches[0])


def _tree_bytes(path: Path) -> int | None:
    if not path.is_dir() or path.is_symlink():
        return None
    stdout = _run("/usr/bin/du", "-s", "-B1", "--", str(path))
    value = stdout.split("\t", 1)[0]
    if not re.fullmatch(r"[0-9]{1,25}", value):
        raise RuntimeError("unexpected filesystem size")
    return int(value)


def _host_disk() -> dict[str, int | float]:
    stat = os.statvfs(PRIVATE)
    if stat.f_blocks <= 0 or not 0 <= stat.f_bfree <= stat.f_blocks:
        raise RuntimeError("invalid disk counters")
    return {"used_pct": round(100 * (stat.f_blocks - stat.f_bfree) / stat.f_blocks, 2),
            "available_bytes": stat.f_bavail * stat.f_frsize}


def main() -> None:
    if os.geteuid() != 0 or os.environ.get("AE_EXPECTED_CURRENT") != CORE_SHA:
        raise RuntimeError("fixed owner-admin read-only gate required")
    if not CURRENT.is_symlink() or os.readlink(CURRENT) != "releases/" + CORE_SHA:
        raise RuntimeError("core release changed; HOLD")
    for unit in UNITS:
        _run("/usr/bin/systemctl", "is-active", "--quiet", unit)
    # Verify cgroup identity; never accept an arbitrary path from the caller.
    cg = _run("/usr/bin/systemctl", "show", "--value", "-p", "ControlGroup",
              "ae-managed-diagnostics.service")
    if cg != "/system.slice/ae-managed-diagnostics.service":
        raise RuntimeError("diagnostic cgroup identity changed; HOLD")
    before = _cpu_usage_usec(CGROUP / "cpu.stat")
    started = time.monotonic()
    time.sleep(3)
    elapsed = time.monotonic() - started
    after = _cpu_usage_usec(CGROUP / "cpu.stat")
    if elapsed < 2.5 or after < before:
        raise RuntimeError("invalid diagnostic CPU sampling window")
    memory = _counter(CGROUP / "memory.current")
    processes = _counter(CGROUP / "pids.current")
    _run("/usr/bin/systemctl", "is-active", "--quiet", "ae-managed-diagnostics.service")
    report = {
        "schema": 1,
        "core_sha_unchanged": CORE_SHA,
        "diagnostic_active": True,
        "cpu_sample_sec": round(elapsed, 3),
        "diagnostic_cpu_pct_of_one_core": round(100 * (after - before) / (elapsed * 1_000_000), 3),
        "diagnostic_cgroup_memory_bytes": memory,
        "diagnostic_cgroup_process_count": processes,
        "disk_bytes": {"diagnostic_v1_code": _tree_bytes(V1),
                       "diagnostic_v2_code": _tree_bytes(V2),
                       "diagnostic_private": _tree_bytes(PRIVATE),
                       "diagnostic_backups": _tree_bytes(BACKUPS)},
        "host_disk": _host_disk(),
        "limits": "one 3-second direct-service sample; not long-term peak or host-wide baseline",
    }
    evidence = Path(os.environ["AE_EVIDENCE"])
    if evidence.is_symlink() or not evidence.is_dir():
        raise RuntimeError("unsafe admin evidence directory")
    body = json.dumps(report, sort_keys=True, separators=(",", ":"))
    (evidence / "diagnostic-resource-7-7.json").write_text(body + "\n", encoding="ascii")
    print("DIAGNOSTIC_RESOURCES_7_7 " + body, flush=True)


if __name__ == "__main__":
    main()
