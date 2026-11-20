"""Offline guards for the metadata-only diagnostic resource collector."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = (Path(__file__).resolve().parents[1] / "ops/admin-scripts"
          / "diagnostic_resource_snapshot_7_7.py")
spec = importlib.util.spec_from_file_location("diagnostic_resources_7_7", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DiagnosticResourcesTests(unittest.TestCase):
    def test_cpu_usage_reads_only_single_aggregate_counter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cpu.stat"
            path.write_text("usage_usec 12345\nuser_usec 5432\nsystem_usec 789\n")
            self.assertEqual(module._cpu_usage_usec(path), 12345)
            path.write_text("usage_usec 2\nusage_usec 3\n")
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                module._cpu_usage_usec(path)
            path.write_text("usage_usec invalid\n")
            with self.assertRaisesRegex(RuntimeError, "missing"):
                module._cpu_usage_usec(path)

    def test_cgroup_counter_rejects_extra_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "counter"
            path.write_text("1024\n")
            self.assertEqual(module._counter(path), 1024)
            path.write_text("1024\nSECRET\n")
            with self.assertRaisesRegex(RuntimeError, "format"):
                module._counter(path)

    def test_disk_size_never_follows_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "target").mkdir()
            (root / "link").symlink_to(root / "target")
            with patch.object(module, "_run", side_effect=AssertionError("du must not run")):
                self.assertIsNone(module._tree_bytes(root / "link"))
                self.assertIsNone(module._tree_bytes(root / "missing"))

    def test_requires_root_and_exact_controller_gate(self):
        with patch.object(module.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "gate"):
                module.main()
        with (patch.object(module.os, "geteuid", return_value=0),
              patch.dict(module.os.environ, {"AE_EXPECTED_CURRENT": "0" * 40})):
            with self.assertRaisesRegex(RuntimeError, "gate"):
                module.main()


if __name__ == "__main__":
    unittest.main()
