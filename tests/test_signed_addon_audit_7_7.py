"""Synthetic read-only audit of the existing signed-release trust root."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


AUDIT = None
if sys.platform.startswith("linux"):
    source = (Path(__file__).resolve().parents[1] / "ops" / "admin-scripts"
              / "audit_signed_addon_gate_7_7.py")
    spec = importlib.util.spec_from_file_location("ae_signed_gate_audit", source)
    assert spec is not None and spec.loader is not None
    AUDIT = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(AUDIT)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only Netcup audit")
class SignedAddonAuditTests(unittest.TestCase):
    def test_private_file_metadata_does_not_read_contents(self):
        with tempfile.TemporaryDirectory(prefix="ae-signed-audit-") as tmp:
            source = Path(tmp) / "signers"
            source.write_text("public test fixture, never report contents", encoding="utf-8")
            source.chmod(0o600)
            self.assertEqual(AUDIT._marker(source, os.geteuid()), "present_private")
            source.chmod(0o666)
            self.assertEqual(AUDIT._marker(source, os.geteuid()), "unsafe_owner_or_mode")
            self.assertEqual(AUDIT._marker(Path(tmp) / "absent", os.geteuid()), "missing")

    def test_reject_changed_live_release_without_contacting_service(self):
        with patch.object(AUDIT, "_active") as service:
            with self.assertRaisesRegex(RuntimeError, "current release changed"):
                AUDIT.inventory("0" * 40, uid=os.geteuid())
            service.assert_not_called()

    def test_report_contains_only_bounded_markers_no_live_claim(self):
        with patch.object(AUDIT.os, "readlink", return_value="releases/" + AUDIT.OLD_SHA), \
             patch.object(AUDIT, "_active", return_value=True), \
             patch.object(AUDIT, "_marker", return_value="missing"):
            result = AUDIT.inventory(AUDIT.OLD_SHA, uid=1001)
            self.assertEqual(result["status"], "SIGNED_ADDON_GATE_INVENTORIED")
            self.assertFalse(result["signer_files_present"])
            self.assertFalse(result["service_started"])
            self.assertTrue(result["read_only_audit"])
            self.assertEqual(result["scope"], "vl-ktk/ai-agent-engine")

    def test_refuses_preexisting_addon_installation(self):
        def marker(path, uid, *, directory=False):
            return "present_private" if path == AUDIT.NEW_UNIT else "missing"
        with patch.object(AUDIT.os, "readlink", return_value="releases/" + AUDIT.OLD_SHA), \
             patch.object(AUDIT, "_active", return_value=True), \
             patch.object(AUDIT, "_marker", marker):
            with self.assertRaisesRegex(RuntimeError, "target no longer empty"):
                AUDIT.inventory(AUDIT.OLD_SHA, uid=1001)


if __name__ == "__main__":
    unittest.main()
