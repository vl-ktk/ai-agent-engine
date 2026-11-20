"""The Vault remediation must never broaden state or credential directories."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "ops/admin-scripts/vault_netcup_repair_code_dirs_7_7a.py"
spec = importlib.util.spec_from_file_location("vault_repair", SOURCE)
assert spec is not None and spec.loader is not None
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class VaultRepairTests(unittest.TestCase):
    def test_fixed_code_directories_do_not_include_keys_data_or_backup(self) -> None:
        paths = [repair.VAULT_CODE_ROOT, repair.VAULT_CODE_ROOT / "releases",
                 repair.RELEASE, repair.RELEASE / "ai_agent_engine"]
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(str(p).startswith("/opt/ai-agent-engine-vault")
                            for p in paths))
        self.assertTrue(all(not any(s in str(p) for s in
                                    ("/etc/", "/var/lib/", "/var/backups/"))
                            for p in paths))

    def test_controller_lease_uses_exact_sha(self) -> None:
        with patch.dict(repair.os.environ, {"AE_EXPECTED_CURRENT": repair.BASELINE}):
            with patch.object(repair.os, "readlink", return_value="releases/foreign"):
                with self.assertRaises(RuntimeError):
                    repair._check_controller()


if __name__ == "__main__":
    unittest.main()
