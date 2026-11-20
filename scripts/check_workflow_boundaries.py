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
    "release-preparation.yml",
    "netcup-owner-release-transfer.yml",
    "netcup-owner-release-activate.yml",
    "netcup-owner-diagnostics-activate.yml",
    "netcup-owner-diagnostics-upgrade-v2.yml",
    "netcup-gitops-status.yml",
    "netcup-owner-admin.yml",
}
RETIRED_BOOTSTRAP = "netcup-one-time-bootstrap.yml"
PUSH_STATUS = "netcup-gitops-status.yml"  # sole approved automatic SSH exception
OWNER_ADMIN = "netcup-owner-admin.yml"  # explicitly authorized 2026-11-19
PRIVILEGED_TRIGGERS = ("pull_request_target", "workflow_run", "repository_dispatch", "schedule")
NONMANUAL_TRIGGERS = ("push", "pull_request", *PRIVILEGED_TRIGGERS)
SECRET_REFERENCE = re.compile(r"\$\{\{\s*secrets\.", re.IGNORECASE)
ACTION_REFERENCE = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)")
MANUAL_CONFIRMATIONS = {
    "netcup-debian13-user-baseline.yml": "BASELINE",
    "netcup-one-time-bootstrap.yml": "BOOTSTRAP",
    "netcup-readonly-access-audit.yml": "AUDIT",
    "netcup-readonly-ansible-ping.yml": "VERIFY",
    "netcup-readonly-system-audit.yml": "SYSTEM_AUDIT",
    "netcup-owner-release-transfer.yml": "TRANSFER_PREPARED_RELEASE",
    "netcup-owner-release-activate.yml": "ACTIVATE_VERIFIED_RELEASE",
    "netcup-owner-diagnostics-activate.yml": "ACTIVATE_SIGNED_DIAGNOSTICS_7_7",
    "netcup-owner-diagnostics-upgrade-v2.yml": "UPGRADE_SIGNED_DIAGNOSTICS_7_7_V2",
}


def inspect_workflow(name: str, source: str) -> list[str]:
    errors: list[str] = []
    # The repository admin setting requires full-length immutable action SHAs.
    # Keep the same guarantee in local CI so a policy toggle is not a bypass.
    for action in ACTION_REFERENCE.findall(source):
        if action.startswith("./"):
            continue
        if "@" not in action or not re.fullmatch(r"[0-9a-f]{40}", action.rsplit("@", 1)[1]):
            errors.append(f"action must be pinned to a full 40-hex commit SHA: {action}")
    # Existing workflows use a multiline top-level permissions mapping.
    matches = re.findall(r"(?m)^permissions:\s*\n((?:  [^\n]*\n)+)", source)
    expected_permissions = ["  contents: read"]
    if name == "netcup-owner-release-transfer.yml":
        # Read access to Actions artifacts is required to fetch the pinned
        # prepared wheel. No write token permission is allowed.
        expected_permissions.append("  actions: read")
    if len(matches) != 1 or matches[0].splitlines() != expected_permissions:
        errors.append("GITHUB_TOKEN permissions differ from reviewed read-only scope")
    if re.search(r"(?m)^\s{2,}permissions\s*:", source):
        errors.append("job-specific permissions overrides are disallowed")
    for trigger in PRIVILEGED_TRIGGERS:
        if re.search(rf"(?m)^  {trigger}\s*:", source):
            errors.append(f"unexpected trigger: {trigger}")

    # A tightly scoped push-triggered workflow may use the existing non-root
    # SSH key only for a fixed read-only status command. Never use this
    # exemption for privileged deployment, command execution, or other files.
    if name == PUSH_STATUS:
        required = (
            "on:\n  push:\n    branches: [main]\n    paths:\n      - 'ops/requests/*.json'",
            "github.actor == 'vl-ktk'",
            "python3 scripts/netcup_status_request.py --github-output",
            """'set -e; systemctl is-active --quiet ae-controller.service; systemctl is-active --quiet ae-backup.timer; systemctl is-active --quiet ae-host-check.timer; echo "all three units active"; readlink /opt/ai-agent-engine/current'""",
            "SHA256:irOFPt5ymeILMXOe0cRyHYwY39XkmwxZ0OJkPh20QJg",
        )
        for token in required:
            if token not in source:
                errors.append("GitOps status safety boundary missing: " + token[:48])
        for trigger in ("pull_request", *PRIVILEGED_TRIGGERS, "workflow_dispatch"):
            if re.search(rf"(?m)^  {trigger}\s*:", source):
                errors.append(f"unexpected status trigger: {trigger}")
        if "sudo " in source or "systemctl restart" in source or "systemctl start" in source:
            errors.append("GitOps status must never elevate privileges or modify services")

    if name == OWNER_ADMIN:
        for marker in (
            "on:\n  push:\n    branches: [main]\n    paths:\n      - 'ops/admin-requests/*.json'",
            "github.repository == 'vl-ktk/ai-agent-engine'",
            "github.ref == 'refs/heads/main'", "github.actor == 'vl-ktk'",
            "github.triggering_actor == 'vl-ktk'", "github.run_attempt == 1",
            "python3 scripts/netcup_admin_request.py --github-output",
            "persist-credentials: false", "python3 -m unittest discover -s tests -q",
            "scripts/netcup_admin_remote.py", "--expected-current",
            "--script-sha256", "--payload-sha",
            "SHA256:irOFPt5ymeILMXOe0cRyHYwY39XkmwxZ0OJkPh20QJg",
            "StrictHostKeyChecking=yes", 'test "$(gh api',
        ):
            if marker not in source:
                errors.append("owner admin boundary missing: " + marker[:60])
        for trigger in ("pull_request", "workflow_dispatch", *PRIVILEGED_TRIGGERS):
            if re.search(rf"(?m)^  {trigger}\s*:", source):
                errors.append("unexpected owner admin trigger: " + trigger)

    if SECRET_REFERENCE.search(source) and name not in (PUSH_STATUS, OWNER_ADMIN):
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

    if name == "netcup-owner-release-activate.yml":
        # Remote sudo activation must always be gated by an independent
        # owner signing identity, not merely by a caller-supplied digest.
        for marker in (
            "secrets.AE_RELEASE_SIGNING_PRIVATE_KEY",
            "ssh-keygen -Y sign",
            "ai-agent-engine-release@vl-ktk",
            "release-intent.json.sig",
            "owner_release_operator.py",
        ):
            if marker not in source:
                errors.append("release signing boundary missing: " + marker)

    if name == "netcup-owner-diagnostics-activate.yml":
        # Additional service only; never use the core release promotion path.
        for marker in (
            "inputs.mode == 'check-signature'",
            "inputs.confirmation == 'VERIFY_SIGNING_KEY'",
            "inputs.mode == 'activate'",
            "github.triggering_actor == 'vl-ktk'",
            "github.run_attempt == 1",
            "secrets.AE_RELEASE_SIGNING_PRIVATE_KEY",
            "ssh-keygen -Y sign", "ssh-keygen -Y verify",
            "60a53cb6f8654700ccb630ef0ad75f3fe1ff0c23",
            "0e04af007375d82daf8f7d3b36c71abb0c784e0b",
            "SHA256:irOFPt5ymeILMXOe0cRyHYwY39XkmwxZ0OJkPh20QJg",
            "StrictHostKeyChecking=yes",
            "diagnostic_addon_operator.py",
            "test \"$(gh api",
        ):
            if marker not in source:
                errors.append("diagnostic signing boundary missing: " + marker)
        for forbidden in ("owner_release_operator.py", "promotion.py", "systemctl restart ae-controller"):
            if forbidden in source:
                errors.append("diagnostic workflow attempts unrelated core promotion")

    if name == "netcup-owner-diagnostics-upgrade-v2.yml":
        for marker in (
            "inputs.mode == 'check-signature'",
            "inputs.confirmation == 'VERIFY_SIGNING_KEY'",
            "inputs.mode == 'upgrade'",
            "github.triggering_actor == 'vl-ktk'",
            "github.run_attempt == 1",
            "secrets.AE_RELEASE_SIGNING_PRIVATE_KEY",
            "ssh-keygen -Y sign", "ssh-keygen -Y verify",
            "3ef07c51ec287a24bd54d8fcea7c40a99e69b569",
            "0e04af007375d82daf8f7d3b36c71abb0c784e0b",
            "SHA256:irOFPt5ymeILMXOe0cRyHYwY39XkmwxZ0OJkPh20QJg",
            "StrictHostKeyChecking=yes", "diagnostic_addon_operator.py",
            'test "$(gh api',
        ):
            if marker not in source:
                errors.append("v2 diagnostic signing boundary missing: " + marker)
        for forbidden in ("owner_release_operator.py", "promotion.py",
                          "systemctl restart ae-controller"):
            if forbidden in source:
                errors.append("v2 diagnostics must never promote the controller")

    if name == "release-preparation.yml":
        if SECRET_REFERENCE.search(source):
            errors.append("release preparation must not receive encrypted repository secrets")
        if not re.search(r"(?m)^  workflow_dispatch\s*:", source):
            errors.append("release preparation must be manually dispatched")
        for trigger in NONMANUAL_TRIGGERS:
            if re.search(rf"(?m)^  {trigger}\s*:", source):
                errors.append(f"release preparation must not use {trigger}")
        job_if = re.search(r"(?m)^    if:\s*(.+)$", source)
        condition = job_if.group(1) if job_if else ""
        for required in (
            "github.ref == 'refs/heads/main'",
            "github.actor == 'vl-ktk'",
            "inputs.confirmation == 'PREPARE_RELEASE'",
        ):
            if required not in condition:
                errors.append(f"release preparation missing owner gate {required}")
        if "persist-credentials: false" not in source:
            errors.append("release preparation must not persist GitHub token during build")
        if "Recheck main HEAD immediately before artifact publication" not in source:
            errors.append("release preparation missing pre-publication HEAD recheck")

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
    print("PASS: workflow sources have SHA-pinned actions, read-only token and manual secret boundaries")
