"""Offline CI security checks never need credentials or provider requests."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.check_public_source import scan_files, tracked_files


class PublicSourceScanTests(unittest.TestCase):
    def test_repository_public_source_has_no_high_confidence_findings(self):
        root = Path(__file__).resolve().parent.parent
        findings = scan_files(root, tracked_files(root))
        self.assertEqual(findings, ())

    def test_detects_force_added_credential_filenames_and_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            names = ("session.json", ".env.production", "id_ed25519", "secret.pem",
                     "credentials/user.json", "private-keys/auth.txt")
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(scan_files(root, (name,)),
                                     ((name, "credential_file_path"),))

    def test_detects_token_and_private_key_signatures_without_echoing_values(self):
        examples = (
            ("github_token", "ghp_" + "A" * 36),
            ("github_token", "github_pat_" + "B" * 40),
            ("openai_api_key", "sk-" + "C" * 30),
            ("aws_access_key", "AKIA" + "D" * 16),
            ("slack_token", "xoxb-" + "1234-" * 6),
            ("private_key_block", "-----BEGIN " + "PRIVATE KEY-----"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "sample.txt"
            for signature, secret in examples:
                with self.subTest(signature=signature):
                    file_path.write_text(secret, encoding="utf-8")
                    findings = scan_files(root, ("sample.txt",))
                    self.assertEqual(findings, (("sample.txt", signature),))
                    self.assertNotIn(secret, repr(findings))

    def test_ordinary_docs_and_synthetic_placeholder_do_not_trigger(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.txt").write_text(
                "Never commit session.json; use fake-token-only in tests.\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_files(root, ("sample.txt",)), ())

    def test_reject_symlinks_binary_oversized_and_traversal(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as other:
            root = Path(directory)
            (root / "large.txt").write_bytes(b"0" * 2_000_001)
            (root / "binary.txt").write_bytes(b"\xff")
            (root / "alias.txt").symlink_to(Path(other) / "outside.txt")
            findings = scan_files(root, ("large.txt", "binary.txt", "alias.txt", "../escape"))
            self.assertEqual(findings, (
                ("large.txt", "oversized_public_file"),
                ("binary.txt", "unreadable_or_binary_file"),
                ("alias.txt", "unsafe_tracked_file"),
                ("../escape", "invalid_tracked_path"),
            ))


if __name__ == "__main__":
    unittest.main()
