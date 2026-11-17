"""Stage 4.2/7.2 offline release tests; all Git operations stay in temporary repos."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from scripts.release_preflight import (
    ReleaseError, assert_main_unchanged, resolve_release,
    scan_release_tree, write_manifest, main,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


class ReleasePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Offline CI")
        git(self.repo, "config", "user.email", "ci@example.invalid")
        file = self.repo / "source.txt"
        file.write_text("first\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "first")
        self.first = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "v0.1.0")
        git(self.repo, "switch", "-c", "rogue")
        file.write_text("rogue\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "rogue")
        self.rogue = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "main")
        file.write_text("second\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "second")
        self.main_head = git(self.repo, "rev-parse", "HEAD")

    def resolve(self, selected_ref, *, observed=None, expected=None, workflow=None):
        return resolve_release(
            self.repo, selected_ref,
            expected or self.main_head, workflow or self.main_head,
            observed or self.main_head,
        )

    def test_exact_current_or_ancestor_commit_succeeds(self):
        self.assertEqual(self.resolve(self.main_head), self.main_head)
        self.assertEqual(self.resolve(self.first), self.first)

    def test_exact_tag_resolves_immutable_commit(self):
        self.assertEqual(self.resolve("refs/tags/v0.1.0"), self.first)

    def test_branch_or_ref_expression_is_not_a_release_selection(self):
        for bad in ("main", "HEAD", "refs/heads/main", "refs/tags/../../etc",
                    "refs/tags/a.lock", "refs/tags/a/b", "a" * 40, "0" * 41):
            with self.subTest(value=bad), self.assertRaises(ReleaseError):
                self.resolve(bad)

    def test_unmerged_commit_is_rejected(self):
        with self.assertRaisesRegex(ReleaseError, "not an ancestor"):
            self.resolve(self.rogue)

    def test_stale_remote_or_workflow_head_is_rejected(self):
        with self.assertRaisesRegex(ReleaseError, "changed"):
            self.resolve(self.first, observed=self.first)
        with self.assertRaisesRegex(ReleaseError, "Workflow commit"):
            self.resolve(self.first, workflow=self.first)
        with self.assertRaisesRegex(ReleaseError, "Checked-out"):
            self.resolve(self.first, expected=self.first, observed=self.first, workflow=self.first)

    def test_remote_sha_validation(self):
        assert_main_unchanged(self.first, self.first)
        with self.assertRaises(ReleaseError):
            assert_main_unchanged(self.first, "not-a-sha")

    def test_cli_writes_exact_pinned_sha_to_github_output(self):
        target = Path(self.temporary.name) / "output"
        target.write_text("", encoding="utf-8")
        code = main([
            "resolve", "--repo", str(self.repo),
            "--selected-ref", "refs/tags/v0.1.0",
            "--expected-main", self.main_head,
            "--workflow-head", self.main_head,
            "--observed-main", self.main_head,
            "--github-output", str(target),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(target.read_text(), f"source_sha={self.first}\n")

    def test_selected_source_scanner_checks_tracked_paths(self):
        scan_release_tree(self.repo)
        credential = self.repo / "id_ed25519"
        credential.write_text("synthetic placeholder", encoding="utf-8")
        git(self.repo, "add", "id_ed25519")
        with self.assertRaisesRegex(ReleaseError, "secret scan"):
            scan_release_tree(self.repo)

    def test_manifest_is_pinned_and_non_overwriting(self):
        release_dir = Path(self.temporary.name) / "release"
        release_dir.mkdir()
        wheel = release_dir / "ai_agent_engine-0.3.7a0-py3-none-any.whl"
        wheel.write_bytes(b"fake package for manifest-only test")
        manifest = write_manifest(
            release_dir, wheel, self.first, "refs/tags/v0.1.0", self.main_head,
        )
        data = json.loads(manifest.read_text())
        self.assertEqual(data["source_commit"], self.first)
        self.assertEqual(data["validated_main_head"], self.main_head)
        self.assertEqual(data["wheel_sha256"], hashlib.sha256(wheel.read_bytes()).hexdigest())
        self.assertFalse(data["controller_service_activated"])
        with self.assertRaisesRegex(ReleaseError, "overwritten"):
            write_manifest(release_dir, wheel, self.first, self.first, self.main_head)

    def test_manifest_rejects_wheel_outside_directory(self):
        release_dir = Path(self.temporary.name) / "release"
        release_dir.mkdir()
        other = Path(self.temporary.name) / "outside.whl"
        other.write_bytes(b"synthetic wheel")
        with self.assertRaises(ReleaseError):
            write_manifest(release_dir, other, self.first, self.first, self.main_head)


if __name__ == "__main__":
    unittest.main()
