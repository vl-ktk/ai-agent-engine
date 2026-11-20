"""No-host-mutation checks of the exact owner-admin 7.7 service proposal."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = (Path(__file__).resolve().parents[1] / "ops" / "admin-scripts"
          / "activate_managed_diagnostics_7_7.py")
SPEC = importlib.util.spec_from_file_location("ae_activate_7_7", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ACTIVATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACTIVATE)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only owner host installer")
class ManagedActivationAdminTests(unittest.TestCase):
    def test_exact_reviewed_new_runtime_module_digest_is_pinned(self):
        source = (Path(__file__).resolve().parents[1] / "ai_agent_engine"
                  / "diagnostic_managed_runtime.py")
        self.assertEqual(sha256(source.read_bytes()).hexdigest(), ACTIVATE.RUNTIME_SHA)
        _, files = ACTIVATE._reviewed_source()
        self.assertIn(source, files)

    def test_separate_systemd_unit_never_replaces_live_release(self):
        unit = ACTIVATE.unit_text()
        self.assertIn("User=ae-runtime\n", unit)
        self.assertIn("Restart=no\n", unit)
        self.assertIn("RuntimeDirectoryMode=0700\n", unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", unit)
        self.assertIn("PrivateNetwork=yes\n", unit)
        self.assertIn("ProtectSystem=strict\n", unit)
        self.assertIn("ReadWritePaths=/var/lib/ai-agent-engine-diagnostics", unit)
        self.assertIn("MemoryMax=192M\n", unit)
        self.assertIn("CPUQuota=20%\n", unit)
        self.assertIn("ExecStart=/usr/bin/python3 -I ", unit)
        self.assertNotIn("ae-controller.service", unit)
        self.assertNotIn("/opt/ai-agent-engine/current", unit)
        self.assertEqual(ACTIVATE.UNIT.name, "ae-managed-diagnostics.service")
        self.assertEqual(ACTIVATE.PROJECT_SHA,
                         "741fc1a4e62b52922611e1e28d4805d727dffe4d")

    def test_launcher_only_uses_frozen_package_not_mutable_project(self):
        launcher = ACTIVATE.launcher_text()
        self.assertIn(repr(str(ACTIVATE.CODE)), launcher)
        self.assertIn("from ai_agent_engine.diagnostic_managed_runtime import main", launcher)
        self.assertNotIn(str(ACTIVATE.PRIVATE_ROOT / "workspaces"), launcher)

    def test_refuse_changed_current_release_before_any_mutation(self):
        with patch.object(ACTIVATE.os, "readlink", return_value="releases/unexpected"):
            with self.assertRaisesRegex(RuntimeError, "live_release_changed"):
                ACTIVATE._baseline(ACTIVATE.LIVE_SHA)
        with self.assertRaisesRegex(RuntimeError, "live_release_changed"):
            ACTIVATE._baseline("0" * 40)

    def test_refuse_backup_dropin_change(self):
        with patch.object(ACTIVATE.os, "readlink",
                          return_value="releases/" + ACTIVATE.LIVE_SHA), \
             patch.object(ACTIVATE, "_systemctl"), \
             patch.object(ACTIVATE, "_regular"), \
             patch.object(ACTIVATE, "_hash", return_value=ACTIVATE.BACKUP_SHA), \
             patch.object(Path, "read_text", return_value="corrupted backup drop-in"):
            with self.assertRaisesRegex(RuntimeError, "diagnostic_backup_changed"):
                ACTIVATE._baseline(ACTIVATE.LIVE_SHA)

    def test_new_files_are_exclusive_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="ae-77admin-") as tmp:
            target = Path(tmp) / "new-file"
            ACTIVATE._exclusive_file(target, b"first", 0o600)
            with self.assertRaises(FileExistsError):
                ACTIVATE._exclusive_file(target, b"overwrite", 0o600)
            self.assertEqual(target.read_bytes(), b"first")


if __name__ == "__main__":
    unittest.main()
