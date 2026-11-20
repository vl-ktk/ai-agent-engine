"""Static and synthetic checks for the narrowly scoped owner-admin installer."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "ops/admin-scripts/vault_netcup_install_7_7a.py"
spec = importlib.util.spec_from_file_location("vault_installer", SOURCE)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class VaultInstallTests(unittest.TestCase):
    def test_fixed_units_only_and_no_public_listener(self) -> None:
        units = installer._unit_specs("a" * 40, "ae-runtime")
        self.assertEqual(set(units), set(installer.UNITS))
        socket = units["ae-vault.socket"].decode()
        self.assertIn("ListenStream=/run/ae-vault.sock", socket)
        self.assertIn("SocketMode=0660", socket)
        self.assertIn("SocketGroup=ae-runtime", socket)
        self.assertNotIn("0.0.0.0", socket)
        self.assertNotIn("ListenStream=80", socket)
        service = units["ae-vault.service"].decode()
        self.assertIn("User=ae-vault", service)
        self.assertIn("StateDirectoryMode=0700", service)
        self.assertIn("LoadCredentialEncrypted=vault-master:", service)
        self.assertIn("/" + "a" * 40 + "/run.py serve", service)
        backup = units["ae-vault-backup.service"].decode()
        self.assertIn("User=ae-vault", backup)
        self.assertIn("LoadCredentialEncrypted=vault-backup:", backup)
        self.assertIn("/run.py backup", backup)
        self.assertIn("OnCalendar=daily", units["ae-vault-backup.timer"].decode())

    def test_new_file_does_not_replace_any_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "unit"
            with patch.object(installer.os, "chown"):
                installer._write_new(target, b"first")
                with self.assertRaises(FileExistsError):
                    installer._write_new(target, b"second")
            self.assertEqual(target.read_bytes(), b"first")

    def test_controller_change_stops_install_without_side_effects(self) -> None:
        with patch.dict(installer.os.environ, {"AE_EXPECTED_CURRENT": installer.BASELINE}):
            with patch.object(installer.os, "readlink", return_value="releases/unexpected"):
                with self.assertRaisesRegex(RuntimeError, "baseline changed"):
                    installer._expected_controller()


if __name__ == "__main__":
    unittest.main()
