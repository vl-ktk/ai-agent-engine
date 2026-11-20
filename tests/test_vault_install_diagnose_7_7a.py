"""The post-install audit must expose only short, whitelisted metadata."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "ops/admin-scripts/vault_install_diagnose_7_7a.py"
spec = importlib.util.spec_from_file_location("vault_diagnose", SOURCE)
assert spec is not None and spec.loader is not None
diagnose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose)


class VaultDiagnoseTests(unittest.TestCase):
    def test_metadata_never_prints_contents(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "fake-secret-credential"
            path.write_bytes(b"ONLY-SYNTHETIC-KEY-NEVER-OUTPUT")
            result = diagnose._metadata(path)
            self.assertTrue(result["present"])
            self.assertNotIn("ONLY-SYNTHETIC", repr(result))
            self.assertNotIn("size", result)

    def test_unit_properties_allowlist_and_sanitize_unknown_values(self) -> None:
        class Fake:
            stdout = ("ActiveState=failed\nResult=exit-code\n"
                      "ExecMainStatus=1\nStatusText=SENSITIVE\n"
                      "SubState=SENSITIVE-VALUE\n")
        with patch.object(diagnose.subprocess, "run", return_value=Fake()):
            state = diagnose._unit("ae-vault.service")
        self.assertEqual(state["ActiveState"], "failed")
        self.assertEqual(state["Result"], "exit-code")
        self.assertEqual(state["ExecMainStatus"], "1")
        self.assertEqual(state["SubState"], "other")
        self.assertNotIn("StatusText", state)


if __name__ == "__main__":
    unittest.main()
