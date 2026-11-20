"""Offline review of the fixed, rollback-capable Netcup backup installer."""

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


if os.name != "posix":
    raise unittest.SkipTest("Netcup backup installer requires POSIX")

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "upgrade_diagnostic_backup_v17_7_11",
    ROOT / "ops/admin-scripts/upgrade_diagnostic_backup_v17_7_11.py",
)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class InstallV17HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hook = Path(self.temp.name) / "backup.py"
        self.hook.write_bytes(b"historical original")
        self.hook.chmod(0o755)

    def test_new_hook_is_exactly_pinned(self):
        source = ROOT / "ai_agent_engine/diagnostic_backup_v17.py"
        self.assertEqual(INSTALLER._hash(source.read_bytes()), INSTALLER.NEW_HOOK_SHA256)
        self.assertNotEqual(INSTALLER.NEW_HOOK_SHA256, INSTALLER.OLD_HOOK_SHA256)

    def test_atomic_install_and_recover_original_with_same_mode(self):
        before = self.hook.read_bytes()
        with patch.object(INSTALLER, "HOOK", self.hook):
            INSTALLER._atomic_install(b"reviewed replacement", "a" * 32)
            self.assertEqual(self.hook.read_bytes(), b"reviewed replacement")
            self.assertEqual(stat.S_IMODE(self.hook.stat().st_mode), 0o755)
            self.assertFalse((self.hook.parent / (".ae-diagnostic-backup-" + "a" * 32 + ".tmp")).exists())
            INSTALLER._atomic_install(before, "b" * 32)
            self.assertEqual(self.hook.read_bytes(), before)

    def test_invalid_installer_payload_does_not_change_original(self):
        with patch.object(INSTALLER, "HOOK", self.hook):
            with self.assertRaisesRegex(RuntimeError, "invalid reviewed installer payload"):
                INSTALLER._atomic_install(b"", "c" * 32)
        self.assertEqual(self.hook.read_bytes(), b"historical original")

    def test_known_original_metadata_refusal_does_not_open_untrusted_file(self):
        self.hook.chmod(0o600)
        with patch.object(INSTALLER, "HOOK", self.hook):
            with self.assertRaisesRegex(RuntimeError, "metadata changed"):
                INSTALLER._read_installed()


if __name__ == "__main__":
    unittest.main()
