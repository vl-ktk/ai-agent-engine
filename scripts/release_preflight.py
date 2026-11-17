#!/usr/bin/env python3
"""Offline, fail-closed release source verification and artifact manifest.

Does not connect to a VPS, run a service, read credentials, or mutate release data.
The GitHub workflow checks current main through the read-only GitHub API and
passes the observed SHA here. Selected refs are resolved with Git list arguments,
never executed as shell text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from scripts.check_public_source import scan_files, tracked_files


SHA = re.compile(r"[0-9a-f]{40}\Z")
TAG = re.compile(r"refs/tags/[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


class ReleaseError(ValueError):
    """Release safety prerequisite was not met."""


def require_sha(value: str, label: str) -> str:
    if not SHA.fullmatch(value):
        raise ReleaseError(f"{label} must be an exact lowercase 40-character Git SHA")
    return value


def assert_main_unchanged(observed: str, expected: str) -> None:
    if require_sha(observed, "observed remote main") != require_sha(expected, "expected main"):
        raise ReleaseError("Remote main HEAD changed; release preparation refused")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseError("Git verification failed; refusing release")
    return result.stdout.strip()


def resolve_release(
    repo: Path, selected_ref: str, expected_main: str,
    workflow_head: str, observed_main: str,
) -> str:
    """Return one immutable commit, only when all main/selection gates pass."""
    require_sha(expected_main, "expected main")
    require_sha(workflow_head, "workflow HEAD")
    assert_main_unchanged(observed_main, expected_main)
    if workflow_head != expected_main:
        raise ReleaseError("Workflow commit does not match approved main HEAD")
    if git(repo, "rev-parse", "--verify", "HEAD") != expected_main:
        raise ReleaseError("Checked-out source differs from approved main HEAD")

    if SHA.fullmatch(selected_ref):
        resolved = git(repo, "rev-parse", "--verify", f"{selected_ref}^{{commit}}")
        if resolved != selected_ref:
            raise ReleaseError("Selected SHA does not identify a commit")
    elif TAG.fullmatch(selected_ref) and ".." not in selected_ref and not selected_ref.endswith(".lock"):
        git(repo, "check-ref-format", selected_ref)
        resolved = git(repo, "rev-parse", "--verify", f"{selected_ref}^{{commit}}")
        require_sha(resolved, "resolved tag commit")
    else:
        raise ReleaseError("Selection must be an exact 40-character commit or refs/tags/<safe-tag>")

    ancestry = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", resolved, expected_main],
        capture_output=True, check=False,
    )
    if ancestry.returncode != 0:
        raise ReleaseError("Selected commit is not an ancestor of approved main")
    return resolved


def scan_release_tree(repo: Path) -> None:
    findings = scan_files(repo, tracked_files(repo))
    if findings:
        for name, rule in findings:
            print(f"BLOCKED selected source: {name!r} ({rule})", file=sys.stderr)
        raise ReleaseError("Selected release source failed the offline secret scan")


def write_manifest(
    directory: Path, wheel: Path, commit: str, selected_ref: str,
    expected_main: str,
) -> Path:
    require_sha(commit, "release commit")
    require_sha(expected_main, "expected main")
    if not (SHA.fullmatch(selected_ref) or
            (TAG.fullmatch(selected_ref) and ".." not in selected_ref
             and not selected_ref.endswith(".lock"))):
        raise ReleaseError("Invalid selected ref")
    release_dir = directory.resolve(strict=True)
    wheel_file = wheel.resolve(strict=True)
    if (wheel_file.parent != release_dir or wheel_file.suffix != ".whl"
            or not wheel_file.is_file() or wheel_file.is_symlink()):
        raise ReleaseError("Wheel must be one regular file directly in the release output directory")
    name = wheel_file.name
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", name):
        raise ReleaseError("Unexpected wheel filename")
    size = wheel_file.stat().st_size
    if size <= 0 or size > 100_000_000:
        raise ReleaseError("Unexpected wheel size")
    digest = hashlib.file_digest(wheel_file.open("rb"), "sha256").hexdigest()
    manifest = {
        "schema": 1,
        "mode": "prepared_only_no_vps_deploy",
        "source_commit": commit,
        "source_ref": selected_ref,
        "validated_main_head": expected_main,
        "wheel_file": name,
        "wheel_bytes": size,
        "wheel_sha256": digest,
        "controller_service_activated": False,
    }
    target = release_dir / "release-manifest.json"
    if target.exists():
        raise ReleaseError("Existing release manifest must not be overwritten")
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    resolve = actions.add_parser("resolve")
    resolve.add_argument("--repo", type=Path, default=Path("."))
    resolve.add_argument("--selected-ref", required=True)
    resolve.add_argument("--expected-main", required=True)
    resolve.add_argument("--workflow-head", required=True)
    resolve.add_argument("--observed-main", required=True)
    resolve.add_argument("--github-output", type=Path, required=True)
    scan = actions.add_parser("scan")
    scan.add_argument("--repo", required=True, type=Path)
    manifest = actions.add_parser("manifest")
    manifest.add_argument("--release-dir", required=True, type=Path)
    manifest.add_argument("--wheel", required=True, type=Path)
    manifest.add_argument("--commit", required=True)
    manifest.add_argument("--selected-ref", required=True)
    manifest.add_argument("--expected-main", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            commit = resolve_release(
                args.repo, args.selected_ref, args.expected_main,
                args.workflow_head, args.observed_main,
            )
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"source_sha={commit}\n")
            print("PASS: main unchanged, selected commit pinned and reachable")
        elif args.command == "scan":
            scan_release_tree(args.repo)
            print("PASS: selected release source secret-scan")
        elif args.command == "manifest":
            path = write_manifest(
                args.release_dir, args.wheel, args.commit,
                args.selected_ref, args.expected_main,
            )
            print(f"PASS: release manifest written at {path}")
    except (ReleaseError, OSError, subprocess.SubprocessError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
