"""Offline validation of the GitOps read-only command transport."""
from pathlib import Path
import json
import subprocess
from tempfile import TemporaryDirectory
import unittest

from scripts.netcup_status_request import InvalidRequest, validate_request


REQUEST_ID = "0123456789abcdef0123456789abcdef"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class StatusRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Test User")
        git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "seed").write_text("start\n")
        git(self.root, "add", "seed")
        git(self.root, "commit", "-qm", "seed")

    def request(self, payload=None, additional=False):
        folder = self.root / "ops" / "requests"
        folder.mkdir(parents=True, exist_ok=True)
        data = payload if payload is not None else {
            "schema": 1, "operation": "status", "request_id": REQUEST_ID,
            "server_ipv4": "8.8.8.8",
        }
        (folder / f"{REQUEST_ID}.json").write_text(json.dumps(data))
        git(self.root, "add", "ops/requests")
        if additional:
            (self.root / "extra").write_text("not a request")
            git(self.root, "add", "extra")
        git(self.root, "commit", "-qm", "request")

    def test_accept_status_request(self):
        self.request()
        self.assertEqual(validate_request(self.root), (REQUEST_ID, "8.8.8.8"))

    def test_refuse_changed_extra_file(self):
        self.request(additional=True)
        with self.assertRaises(InvalidRequest):
            validate_request(self.root)

    def test_refuse_command_injection_and_unexpected_operation(self):
        self.request({"schema": 1, "operation": "shell",
                      "request_id": REQUEST_ID,
                      "server_ipv4": "8.8.8.8; touch /tmp/pwned"})
        with self.assertRaises(InvalidRequest):
            validate_request(self.root)

    def test_refuse_nonpublic_ip(self):
        self.request({"schema": 1, "operation": "status",
                      "request_id": REQUEST_ID,
                      "server_ipv4": "127.0.0.1"})
        with self.assertRaises(InvalidRequest):
            validate_request(self.root)

    def test_refuse_duplicate_keys(self):
        folder = self.root / "ops" / "requests"
        folder.mkdir(parents=True)
        (folder / f"{REQUEST_ID}.json").write_text(
            '{"schema":1,"schema":1,"operation":"status",'
            f'"request_id":"{REQUEST_ID}","server_ipv4":"8.8.8.8"}}'
        )
        git(self.root, "add", "ops/requests")
        git(self.root, "commit", "-qm", "bad request")
        with self.assertRaises(InvalidRequest):
            validate_request(self.root)

    def test_refuse_symlink_request(self):
        folder = self.root / "ops" / "requests"
        folder.mkdir(parents=True)
        (folder / f"{REQUEST_ID}.json").symlink_to(self.root / "seed")
        git(self.root, "add", "ops/requests")
        git(self.root, "commit", "-qm", "bad request")
        with self.assertRaises(InvalidRequest):
            validate_request(self.root)


if __name__ == "__main__":
    unittest.main()
