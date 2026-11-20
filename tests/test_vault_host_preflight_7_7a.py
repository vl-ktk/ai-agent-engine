"""Pure source checks: vault preflight cannot expose credential values."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from contextlib import redirect_stdout
import io
import stat
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ops/admin-scripts/vault_host_preflight_7_7a.py"
SPEC = importlib.util.spec_from_file_location("ae_vault_host_preflight", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VaultPreflightTests(unittest.TestCase):
    def test_main_requires_exact_live_sha_before_inspection(self) -> None:
        output = io.StringIO()
        with (patch.object(MODULE.os, "geteuid", return_value=0),
              patch.object(MODULE.os, "getenv", return_value="unexpected"),
              patch.object(MODULE.os, "readlink") as readlink,
              redirect_stdout(output)):
            self.assertEqual(MODULE.main(), 1)
            readlink.assert_not_called()
            self.assertIn("HOLD_BASELINE", output.getvalue())

    def test_missing_path_has_no_names_or_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ae-vault-preflight-") as tmp:
            meta = MODULE._safe_metadata(Path(tmp) / "missing-private-token")
            self.assertEqual(meta, {"exists": False})

    def test_existing_secret_file_metadata_never_reads_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ae-vault-preflight-") as tmp:
            path = Path(tmp) / "private-token"
            value = "SYNTHETIC_TOKEN_NEVER_PRINT"
            path.write_text(value)
            path.chmod(0o600)
            result = MODULE._safe_metadata(path)
            self.assertEqual(result["type"], "file")
            self.assertTrue(result["private"])
            self.assertNotIn(value, repr(result))
            self.assertNotIn(path.name, repr(result))

    def test_other_file_modes_report_not_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ae-vault-preflight-") as tmp:
            path = Path(tmp) / "shared"
            path.write_bytes(b"synthetic")
            path.chmod(0o644)
            self.assertFalse(MODULE._safe_metadata(path)["private"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
