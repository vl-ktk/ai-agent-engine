#!/usr/bin/env python3
"""Fail CI if reviewed GitHub Actions least-privilege boundaries drift.

This is a source-level audit, NOT a replacement for GitHub branch protection,
Actions repository settings, or a separate owner's approval.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
APPROVED_WORKFLOWS = {
    "ci.yml",
    "netcup-debian13-user-baseline.yml",
    "netcup-external-network-probe.yml",
    "netcup-one-time-bootstrap.yml",
    "netcup-readonly-access-audit.yml",
    "netcup-readonly-ansible-ping.yml",
    "netcup-readonly-system-audit.yml",
    "vps-automation-checks.yml",
}
RETIRED_BOOTSTRAP = "netcup-one-time-bootstrap.yml"
PRIVILEGED_TRIGGERS = ("pull_request_target", "workflow_run", "repository_dispatch", "schedule")
NONMANUAL_TRIGGERS = ("push", "pull_request", *PRIVILEGED_TRIGGERS)
SECRET_REFERENCE = re.compile(r"\$\{\{\s*secrets\.", re.IGNORECASE)
MANUAL_CONFIRMATIONS = {
    "netcup-debian13-user-baseline.yml": "BASELINE",
    "netcup-one-time-bootstrap.yml": "BOOTSTRAP",
    "netcup-readonly-access-audit.yml": "AUDIT",
    "netcup-readonly-ansible-ping.yml": "VERIFY",
    "netcup-readonly-system-audit.yml": "SYSTEM_AUDIT",
}


def inspect_workflow(name: str, source: str) -> list[str]:
    errors: list[str] = []
    # Existing workflows use a multiline top-level permissions mapping.
    matches = re.findall(r"(?m)^permissions:\s*\n((?:  [^\n]*\n)+)", source)
    if len(matches) != 1 or matches[0].splitlines() != ["  contents: read"]:
        errors.append("GITHUB_TOKEN permissions must be top-level contents: read only")
    if re.search(r"(?m)^\s{2,}permissions\s*:", source):
        errors.append("job-specific permissions overrides are disallowed")
    for trigger in PRIVILEGED_TRIGGERS:
        if re.search(rf"(?m)^  {trigger}\s*:", source):
            errors.append(f"unexpected trigger: {trigger}")

    if SECRET_REFERENCE.search(source):
        if not re.search(r"(?m)^  workflow_dispatch\s*:", source):
            errors.append("secret-bearing workflow must require manual dispatch")
        for trigger in NONMANUAL_TRIGGERS:
            if re.search(rf"(?m)^  {trigger}\s*:", source):
                errors.append(f"secret-bearing workflow must not use {trigger}")
        if name not in MANUAL_CONFIRMATIONS:
            errors.append("unreviewed workflow references encrypted secrets")
        else:
            job_if = re.search(r"(?m)^    if:\s*(.+)$", source)
            condition = job_if.group(1) if job_if else ""
            for required in (
                "github.ref == 'refs/heads/main'",
                "github.actor == 'vl-ktk'",
                f"inputs.confirmation == '{MANUAL_CONFIRMATIONS[name]}'",
            ):
                if required not in condition:
                    errors.append(f"manual secret job condition missing {required}")

    if name == RETIRED_BOOTSTRAP:
        if not re.search(r"(?m)^    if:\s*\$\{\{\s*false\s*&&", source):
            errors.append("retired root/password bootstrap must remain hard-disabled")
    return errors


def inspect_repository(workflow_dir: Path = WORKFLOWS) -> list[str]:
    errors: list[str] = []
    files = {p.name for p in workflow_dir.glob("*.yml")}
    for unreviewed in sorted(files - APPROVED_WORKFLOWS):
        errors.append(f"{unreviewed}: unreviewed workflow")
    for missing in sorted(APPROVED_WORKFLOWS - files):
        errors.append(f"{missing}: approved workflow missing")
    for name in sorted(files & APPROVED_WORKFLOWS):
        source = (workflow_dir / name).read_text(encoding="utf-8")
        errors.extend(f"{name}: {issue}" for issue in inspect_workflow(name, source))
    return errors


if __name__ == "__main__":
    problems = inspect_repository()
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        sys.exit(1)
    print("PASS: workflow sources have reviewed read-only token and manual secret boundaries")
