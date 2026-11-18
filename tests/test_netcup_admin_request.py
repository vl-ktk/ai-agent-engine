"""Real Git tests for rejecting unreviewed or injected root-job requests."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.netcup_admin_request import validate
from scripts.check_workflow_boundaries import inspect_workflow


class AdminRequestTests(unittest.TestCase):
    @contextmanager
    def repository(self):
        with tempfile.TemporaryDirectory() as temp:
            old = Path.cwd()
            os.chdir(temp)
            try:
                def git(*args):
                    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL, text=True).strip()
                git("init", "-q")
                git("config", "user.name", "Test")
                git("config", "user.email", "test@example.invalid")
                script = Path("ops/admin-scripts/audit.py")
                script.parent.mkdir(parents=True)
                script.write_text("print('reviewed')\n")
                git("add", ".")
                git("commit", "-qm", "reviewed source")
                doc = {"schema": 1, "request_id": "a" * 32, "source_sha": git("rev-parse", "HEAD"),
                       "expected_current": "b" * 40, "script": str(script).replace("\\", "/"),
                       "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
                path = Path("ops/admin-requests", doc["request_id"] + ".json")
                path.parent.mkdir()
                yield git, doc, path, script
            finally:
                os.chdir(old)

    def commit_request(self, git, doc, path):
        path.write_text(json.dumps(doc))
        git("add", ".")
        git("commit", "-qm", "owner request")

    def test_accept_exact_previously_committed_script(self):
        with self.repository() as (git, doc, path, script):
            self.commit_request(git, doc, path)
            self.assertEqual(validate(), doc)

    def test_reject_script_modified_in_request_commit(self):
        with self.repository() as (git, doc, path, script):
            script.write_text("print('unreviewed')")
            doc["script_sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
            self.commit_request(git, doc, path)
            with self.assertRaises(ValueError):
                validate()

    def test_reject_injected_path_stale_source_digest_and_unknown_fields(self):
        for key, value in (("script", "ops/admin-scripts/a.py; id"), ("source_sha", "c" * 40),
                           ("script_sha256", "0" * 64), ("command", "sudo id"),
                           ("expected_current", "x; id")):
            with self.subTest(key=key), self.repository() as (git, doc, path, script):
                doc[key] = value
                self.commit_request(git, doc, path)
                with self.assertRaises(ValueError):
                    validate()

    def test_reject_duplicate_fields(self):
        with self.repository() as (git, doc, path, script):
            path.write_text(json.dumps(doc)[:-1] + ',"schema":1}')
            git("add", ".")
            git("commit", "-qm", "duplicate request")
            with self.assertRaises(ValueError):
                validate()

    def test_owner_workflow_guards(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / ".github/workflows/netcup-owner-admin.yml").read_text()
        self.assertEqual(inspect_workflow("netcup-owner-admin.yml", source), [])
        for marker in ("github.actor == 'vl-ktk'", "github.triggering_actor == 'vl-ktk'",
                       "github.run_attempt == 1", "StrictHostKeyChecking=yes", "--payload-sha"):
            changed = source.replace(marker, "REMOVED")
            self.assertTrue(inspect_workflow("netcup-owner-admin.yml", changed))
        self.assertTrue(inspect_workflow("netcup-owner-admin.yml", source + "\non:\n  pull_request:\n"))
