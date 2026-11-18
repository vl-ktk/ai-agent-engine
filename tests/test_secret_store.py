"""POSIX-only synthetic tests: no real accounts, network or credentials."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_agent_engine.secret_store import (
    MAX_SECRET_BYTES,
    SecretAccessError,
    SecretStore,
    main,
)


@unittest.skipUnless(os.name == "posix", "Linux credential DAC check")
class SecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="ae-secret-store-tests-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "secrets"
        self.root.mkdir(mode=0o750)
        self.store = SecretStore(
            self.root,
            owner_uid=os.getuid(),
            reader_gid=os.getgid(),
            verify_ancestors=False,
        )

    def put(self, name: str = "synthetic", *, payload: bytes = b"PRIVATE_TEST_SENTINEL") -> Path:
        target = self.root / name
        target.write_bytes(payload)
        target.chmod(0o440)
        return target

    def assert_denied(self, name: str = "synthetic") -> None:
        with self.assertRaisesRegex(SecretAccessError, "Credential store unavailable") as error:
            self.store.read(name)
        self.assertNotIn("PRIVATE_TEST_SENTINEL", str(error.exception))
        self.assertNotIn(str(self.root), str(error.exception))

    def test_read_exact_bytes_and_metadata_only(self) -> None:
        self.put(payload=b"PRIVATE_TEST_SENTINEL\x00\xff\n")
        self.assertEqual(self.store.read("synthetic"), b"PRIVATE_TEST_SENTINEL\x00\xff\n")
        self.assertEqual(self.store.audit().file_count, 1)

    def test_empty_store_is_valid(self) -> None:
        self.assertEqual(self.store.audit().file_count, 0)

    def test_missing_name_fails_without_disclosing_name(self) -> None:
        self.assert_denied("does_not_exist")

    def test_names_cannot_escape_directory(self) -> None:
        self.put()
        for invalid in ("../synthetic", "/etc/passwd", "other/name", ".", "", "UPPER", "b" * 65, "space here", "x\x00y"):
            with self.subTest(invalid=invalid):
                self.assert_denied(invalid)

    def test_world_or_group_writable_files_denied(self) -> None:
        secret = self.put()
        for insecure in (0o644, 0o444, 0o640, 0o660, 0o777):
            with self.subTest(mode=oct(insecure)):
                secret.chmod(insecure)
                self.assert_denied()
        secret.chmod(0o440)
        self.assertEqual(self.store.read("synthetic"), b"PRIVATE_TEST_SENTINEL")

    def test_directory_mode_must_be_exact(self) -> None:
        self.put()
        for mode in (0o770, 0o755, 0o700, 0o777):
            with self.subTest(mode=oct(mode)):
                self.root.chmod(mode)
                self.assert_denied()

    def test_symlink_file_not_followed(self) -> None:
        actual = Path(self.scratch.name) / "target"
        actual.write_bytes(b"PRIVATE_TEST_SENTINEL")
        (self.root / "synthetic").symlink_to(actual)
        self.assert_denied()
        with self.assertRaises(SecretAccessError):
            self.store.audit()

    def test_symlink_directory_not_followed(self) -> None:
        different = Path(self.scratch.name) / "alternate"
        different.symlink_to(self.root, target_is_directory=True)
        store = SecretStore(
            different, owner_uid=os.getuid(), reader_gid=os.getgid(),
            verify_ancestors=False,
        )
        with self.assertRaises(SecretAccessError):
            store.audit()

    def test_mismatched_owner_and_group_denied(self) -> None:
        self.put()
        wrong_owner = SecretStore(
            self.root, owner_uid=os.getuid() + 1000, reader_gid=os.getgid(),
            verify_ancestors=False,
        )
        wrong_group = SecretStore(
            self.root, owner_uid=os.getuid(), reader_gid=os.getgid() + 1000,
            verify_ancestors=False,
        )
        for candidate in (wrong_owner, wrong_group):
            with self.assertRaises(SecretAccessError):
                candidate.read("synthetic")

    def test_parent_symlink_or_writable_component_denied(self) -> None:
        self.put()
        guarded = SecretStore(
            self.root, owner_uid=os.getuid(), reader_gid=os.getgid(),
            verify_ancestors=True,
        )
        with self.assertRaises(SecretAccessError):
            guarded.read("synthetic")  # /tmp ancestry is not root-only/private.

    def test_oversized_secret_denied(self) -> None:
        self.put(payload=b"x" * (MAX_SECRET_BYTES + 1))
        self.assert_denied()
        with self.assertRaises(SecretAccessError):
            self.store.audit()

    def test_empty_secret_denied(self) -> None:
        self.put(payload=b"")
        self.assert_denied()

    def test_directory_entry_not_accepted_as_file(self) -> None:
        (self.root / "synthetic").mkdir()
        self.assert_denied()

    def test_fifo_rejected_without_blocking(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo unavailable")
        os.mkfifo(self.root / "synthetic", mode=0o440)
        self.assert_denied()

    def test_cli_smoke_never_prints_marker(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["smoke"]), 0)
        self.assertIn("PASS", output.getvalue())
        self.assertNotIn("synthetic-placeholder", output.getvalue())

    def test_cli_audit_only_reports_count(self) -> None:
        self.put()
        output = io.StringIO()
        with patch("ai_agent_engine.secret_store.SecretStore", return_value=self.store):
            with redirect_stdout(output):
                self.assertEqual(main(["audit"]), 0)
        self.assertIn("(1 files)", output.getvalue())
        self.assertNotIn("synthetic", output.getvalue())
        self.assertNotIn("PRIVATE_TEST_SENTINEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
