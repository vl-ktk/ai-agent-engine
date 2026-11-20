"""Only safe traceback metadata may leave the Netcup offline diagnostic."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

if os.name != "posix":
    raise unittest.SkipTest("Netcup diagnostic requires POSIX")

source = (Path(__file__).resolve().parents[1] /
          "ops/admin-scripts/stage7_11_offline_env_diagnose.py")
spec = importlib.util.spec_from_file_location("stage7_11_offline_env_diagnose", source)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class OfflineMetadataTests(unittest.TestCase):
    def encoded(self, payload):
        return ("STAGE7_11_OFFLINE_METADATA " + json.dumps(payload)).encode()

    def test_known_failures_only_expose_code_location(self):
        evidence = dict(status="OFFLINE_BLOCKED", exception="PermissionError",
                        function="_execute_claimed", line=201,
                        source="task_gpt.py", missing_module="none",
                        private_package_visible=True, source_file_readable=True)
        self.assertEqual(diag._parse_metadata(self.encoded(evidence)), evidence)

    def test_passed_default_is_offline_only(self):
        good = dict(status="OFFLINE_PASS", exception="none",
                    function="none", source="none", line=0,
                    missing_module="none", private_package_visible=True,
                    source_file_readable=True)
        self.assertEqual(diag._parse_metadata(self.encoded(good)), good)
        self.assertIn("live=False", diag._CODE)
        self.assertNotIn("--live", diag._CODE)

    def test_private_path_account_and_extra_fields_rejected(self):
        valid = dict(status="OFFLINE_BLOCKED", exception="ValueError",
                     function="run_acceptance", line=10,
                     source="task_stage7_11_acceptance.py",
                     missing_module="ai_agent_engine", private_package_visible=False,
                     source_file_readable=False)
        for changes in (dict(source="/private/credential.json"),
                        dict(exception="Secret:secret"),
                        dict(line=True), dict(token="secret"),
                        dict(missing_module="/private/token"),
                        dict(private_package_visible="true"),
                        dict(source_file_readable=1)):
            bad = dict(valid, **changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(
                    RuntimeError, "offline metadata untrusted"):
                diag._parse_metadata(self.encoded(bad))

    def test_missing_child_output_is_not_assumed_success(self):
        result = diag._parse_metadata(b"no response")
        self.assertEqual(result["status"], "OFFLINE_BLOCKED")
        self.assertEqual(result["exception"], "ProcessUnavailable")
        self.assertIs(result["private_package_visible"], False)

    def test_missing_module_is_public_identifier_only(self):
        evidence = dict(status="OFFLINE_BLOCKED", exception="ModuleNotFoundError",
                        function="unknown", line=0, source="unknown",
                        missing_module="ai_agent_engine", private_package_visible=False,
                        source_file_readable=False)
        self.assertEqual(diag._parse_metadata(self.encoded(evidence)), evidence)
        self.assertIn("find_spec('ai_agent_engine')", diag._CODE)
        self.assertIn("ModuleNotFoundError", diag._CODE)

    def test_import_introspection_cannot_abort_receipt_before_exception_handler(self):
        self.assertLess(diag._CODE.index("try:"),
                        diag._CODE.index("import importlib.util,os"))
        self.assertIn("report.update(status='OFFLINE_BLOCKED'", diag._CODE)
        self.assertIn('"runtime_file_readable"',
                      Path(diag.__file__).read_text(encoding="utf-8"))
        self.assertIn('"runtime_package_traversable"',
                      Path(diag.__file__).read_text(encoding="utf-8"))

    def test_actual_offline_child_metadata_without_provider_models(self):
        process = subprocess.run(
            [sys.executable, "-c", diag._CODE],
            cwd=source.parents[2], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False)
        report = diag._parse_metadata(process.stdout)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(report["status"], "OFFLINE_PASS")
        self.assertEqual(report["missing_module"], "none")
        self.assertIs(report["private_package_visible"], True)
        self.assertIs(report["source_file_readable"], True)


if __name__ == "__main__":
    unittest.main()
