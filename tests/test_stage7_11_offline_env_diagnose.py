"""Only safe traceback metadata may leave the Netcup offline diagnostic."""

import importlib.util
import json
import os
from pathlib import Path
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
                        source="task_gpt.py")
        self.assertEqual(diag._parse_metadata(self.encoded(evidence)), evidence)

    def test_passed_default_is_offline_only(self):
        good = dict(status="OFFLINE_PASS", exception="none",
                    function="none", source="none", line=0)
        self.assertEqual(diag._parse_metadata(self.encoded(good)), good)
        self.assertIn("live=False", diag._CODE)
        self.assertNotIn("--live", diag._CODE)

    def test_private_path_account_and_extra_fields_rejected(self):
        valid = dict(status="OFFLINE_BLOCKED", exception="ValueError",
                     function="run_acceptance", line=10,
                     source="task_stage7_11_acceptance.py")
        for changes in (dict(source="/private/credential.json"),
                        dict(exception="Secret:secret"),
                        dict(line=True), dict(token="secret")):
            bad = dict(valid, **changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(
                    RuntimeError, "offline metadata untrusted"):
                diag._parse_metadata(self.encoded(bad))

    def test_missing_child_output_is_not_assumed_success(self):
        result = diag._parse_metadata(b"no response")
        self.assertEqual(result["status"], "OFFLINE_BLOCKED")
        self.assertEqual(result["exception"], "ProcessUnavailable")


if __name__ == "__main__":
    unittest.main()
