"""Validate a single GitOps status request from the newest Git commit.

This script NEVER connects to a server. It exports only a validated public IPv4
for the separate, pinned-host read-only SSH workflow.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess


REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
PREFIX = "ops/requests/"


class InvalidRequest(ValueError):
    pass


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, timeout=15,
        stderr=subprocess.DEVNULL,
    ).strip()


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise InvalidRequest("duplicate JSON key")
        obj[key] = value
    return obj


def validate_request(repo: Path) -> tuple[str, str]:
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) != 2:
        raise InvalidRequest("expected a single non-merge commit")
    change_lines = _git(
        repo, "diff", "--name-status", "--no-renames", parents[1], parents[0],
    ).splitlines()
    if len(change_lines) != 1 or not change_lines[0].startswith("A\t"):
        raise InvalidRequest("commit must add exactly one request file")
    path = change_lines[0][2:]
    if not path.startswith(PREFIX) or not path.endswith(".json"):
        raise InvalidRequest("unexpected changed path")
    request_id = path[len(PREFIX):-5]
    if REQUEST_ID.fullmatch(request_id) is None:
        raise InvalidRequest("invalid request identifier")
    root = repo.resolve(strict=True)
    file = root / path
    if file.is_symlink() or not file.is_file() or file.stat().st_size > 1024:
        raise InvalidRequest("request must be a small regular file")
    try:
        doc = json.loads(file.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, UnicodeError, OSError) as exc:
        raise InvalidRequest("invalid JSON request") from exc
    if not isinstance(doc, dict) or set(doc) != {
        "schema", "operation", "request_id", "server_ipv4",
    }:
        raise InvalidRequest("unexpected request fields")
    if type(doc["schema"]) is not int or doc["schema"] != 1:
        raise InvalidRequest("unsupported schema")
    if doc["operation"] != "status" or doc["request_id"] != request_id:
        raise InvalidRequest("only owner-scoped read-only status is supported")
    raw_ip = doc["server_ipv4"]
    if not isinstance(raw_ip, str):
        raise InvalidRequest("IPv4 must be a string")
    try:
        ip = ipaddress.IPv4Address(raw_ip)
    except ipaddress.AddressValueError as exc:
        raise InvalidRequest("invalid IPv4") from exc
    if not ip.is_global or str(ip) != raw_ip:
        raise InvalidRequest("server must be a canonical global IPv4")
    return request_id, raw_ip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        request_id, ip = validate_request(args.repo)
        with args.github_output.open("a", encoding="utf-8") as target:
            target.write(f"request_id={request_id}\nserver_ipv4={ip}\n")
    except (InvalidRequest, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(f"PASS: validated read-only Netcup status request {request_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
