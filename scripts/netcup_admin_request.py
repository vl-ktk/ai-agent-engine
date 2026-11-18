"""Validate an owner-push request; never execute text supplied in JSON."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import subprocess


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request field")
        result[key] = value
    return result


def validate():
    commits = git("rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(commits) != 2:
        raise ValueError("request must have exactly one parent")
    changes = git("diff", "--name-status", "--no-renames", commits[1], commits[0])
    match = re.fullmatch(r"A\tops/admin-requests/([0-9a-f]{32})\.json", changes)
    if not match:
        raise ValueError("request commit must add exactly one admin JSON file")
    request_id = match[1]
    path = Path("ops/admin-requests", request_id + ".json")
    if path.is_symlink() or path.stat().st_size > 2048:
        raise ValueError("invalid request file")
    doc = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if set(doc) != {"schema", "request_id", "source_sha", "expected_current", "script", "script_sha256"}:
        raise ValueError("unexpected request fields")
    if type(doc["schema"]) is not int or doc["schema"] != 1 or doc["request_id"] != request_id:
        raise ValueError("invalid request identity")
    if doc["source_sha"] != commits[1]:
        raise ValueError("script must come from the previously committed main revision")
    if not isinstance(doc["expected_current"], str) or not re.fullmatch(r"[0-9a-f]{40}", doc["expected_current"]):
        raise ValueError("expected_current must be an immutable release SHA")
    if not isinstance(doc["script"], str) or not re.fullmatch(r"ops/admin-scripts/[a-z0-9_-]{1,64}\.py", doc["script"]):
        raise ValueError("only a reviewed committed administrative Python entry point is allowed")
    script = Path(doc["script"])
    if script.is_symlink() or not script.is_file() or script.stat().st_size > 131072:
        raise ValueError("invalid script")
    if hashlib.sha256(script.read_bytes()).hexdigest() != doc["script_sha256"]:
        raise ValueError("script digest mismatch")
    return doc


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    doc = validate()
    with args.github_output.open("a", encoding="utf-8") as output:
        for key in ("request_id", "source_sha", "expected_current", "script", "script_sha256"):
            output.write(f"{key}={doc[key]}\n")
    print("PASS: one owner request, immutable parent source and matching script digest")
