"""Offline-only VM staging tests with fabricated credentials. No SSH or OAuth."""

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_agent_engine import siwc
from ai_agent_engine import vm_credentials as vm


SOURCE_HOST = "urn:uuid:123e4567-e89b-42d3-a456-426614174000"


def fake_session():
    return {
        "issuer": siwc.ISSUER,
        "subject": "fake-user",
        "email": "demo@example.invalid",
        "client_id": "oaiapp_synthetic",
        "ext_agent_host_id": SOURCE_HOST,
        "id_token": "fake.id.token",
        "access_token": "SECRET_ACCESS_SENTINEL",
        "refresh_token": "SECRET_REFRESH_SENTINEL",
        "token_type": "Bearer",
        "scopes": ["openid", "offline_access", "resource.invoke", siwc.DIRECT_SCOPE],
        "sharing_enabled": True,
        "expires_at": 1234567890,
        "saved_at": "2026-11-16T00:00:00+00:00",
    }


class VMCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = siwc.CredentialStore(Path(self.tmp.name) / "vm")

    def prepared_host(self):
        host = vm.prepare_host(self.store)
        self.assertNotEqual(host, SOURCE_HOST)
        return host

    def test_missing_host_check_has_no_side_effects(self):
        with self.assertRaises(vm.VMImportError):
            vm.existing_vm_host(self.store)
        self.assertFalse(self.store.root.exists())

    def test_prepare_host_is_idempotent(self):
        first = self.prepared_host()
        second = vm.prepare_host(self.store)
        self.assertEqual(first, second)
        self.assertEqual(self.store._read("host.json")["ext_agent_host_id"], first)

    def test_dry_run_does_not_write_session(self):
        host = self.prepared_host()
        before = sorted(x.name for x in self.store.root.iterdir())
        vm.inspect_import(self.store, fake_session())
        self.assertEqual(sorted(x.name for x in self.store.root.iterdir()), before)
        self.assertEqual(vm.existing_vm_host(self.store), host)
        self.assertIsNone(self.store.load_session())

    def test_import_preserves_vm_host_and_all_required_tokens(self):
        host = self.prepared_host()
        vm.import_session(self.store, fake_session(), confirmed_host=host)
        saved = self.store.load_session()
        self.assertEqual(saved["ext_agent_host_id"], host)
        self.assertEqual(saved["access_token"], "SECRET_ACCESS_SENTINEL")
        self.assertEqual(saved["refresh_token"], "SECRET_REFRESH_SENTINEL")
        self.assertEqual(saved["id_token"], "fake.id.token")
        self.assertEqual(saved["client_id"], "oaiapp_synthetic")
        if os.name != "nt":
            self.assertEqual((self.store.root / "session.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)

    def test_rejects_second_import_without_overwriting(self):
        host = self.prepared_host()
        vm.import_session(self.store, fake_session(), confirmed_host=host)
        payload = fake_session()
        payload["access_token"] = "new token must not replace"
        with self.assertRaises(vm.VMImportError):
            vm.import_session(self.store, payload, confirmed_host=host)
        self.assertEqual(self.store.load_session()["access_token"], "SECRET_ACCESS_SENTINEL")

    def test_rejects_wrong_host_confirmation_without_mutation(self):
        self.prepared_host()
        with self.assertRaises(vm.VMImportError):
            vm.import_session(self.store, fake_session(), confirmed_host=SOURCE_HOST)
        self.assertIsNone(self.store.load_session())

    def test_rejects_identical_source_and_destination_hosts(self):
        host = self.prepared_host()
        payload = fake_session()
        payload["ext_agent_host_id"] = host
        with self.assertRaises(vm.VMImportError):
            vm.import_session(self.store, payload, confirmed_host=host)
        self.assertIsNone(self.store.load_session())

    def test_rejects_conflicting_pending_registration(self):
        host = self.prepared_host()
        self.store.remember_registration("oaiapp_earlier")
        with self.assertRaises(vm.VMImportError):
            vm.import_session(self.store, fake_session(), confirmed_host=host)
        self.assertIsNone(self.store.load_session())
        self.assertEqual(self.store.client_id(), "oaiapp_earlier")

    def test_compatible_pending_registration_is_reused(self):
        host = self.prepared_host()
        self.store.remember_registration("oaiapp_synthetic")
        vm.import_session(self.store, fake_session(), confirmed_host=host)
        self.assertEqual(self.store.client_id(), "oaiapp_synthetic")
        self.assertIsNone(self.store._read("pending-registration.json"))

    def test_invalid_fields_are_rejected(self):
        host = self.prepared_host()
        mutations = (
            ("issuer", "https://attacker.invalid"),
            ("client_id", "dynamic_agent_client"),
            ("ext_agent_host_id", "urn:uuid:not-valid"),
            ("access_token", ""),
            ("refresh_token", None),
            ("sharing_enabled", False),
            ("token_type", "Other"),
            ("expires_at", float("nan")),
            ("saved_at", "invalid"),
            ("scopes", ["openid"]),
        )
        for key, bad in mutations:
            with self.subTest(field=key):
                payload = fake_session()
                payload[key] = bad
                with self.assertRaises(vm.VMImportError):
                    vm.validate_session(payload, host)
        payload = fake_session()
        payload["extra_field"] = "unsupported"
        with self.assertRaises(vm.VMImportError):
            vm.validate_session(payload, host)

    def test_read_limit_and_json_errors(self):
        with self.assertRaises(vm.VMImportError):
            vm.read_session(BytesIO(b"x" * (vm.MAX_IMPORT_BYTES + 1)))
        with self.assertRaises(vm.VMImportError):
            vm.read_session(BytesIO(b"{malformed"))
        with self.assertRaises(vm.VMImportError):
            vm.read_session(BytesIO(b"\xff"))
        class TerminalStream(BytesIO):
            def isatty(self):
                return True

        with self.assertRaises(vm.VMImportError):
            vm.read_session(TerminalStream(b"{}"))

    def test_main_requires_explicit_apply_and_confirmation(self):
        host = self.prepared_host()
        supplied = json.dumps(fake_session()).encode("utf-8")
        for args in [
            ["import-stdin"],
            ["import-stdin", "--apply"],
            ["import-stdin", "--confirm-vm-host", host],
            ["import-stdin", "--apply", "--confirm-vm-host", SOURCE_HOST],
        ]:
            with self.subTest(args=args):
                with redirect_stderr(StringIO()), redirect_stdout(StringIO()):
                    result = vm.main(args, store=self.store, input_stream=BytesIO(supplied))
                self.assertEqual(result, 1)
                self.assertIsNone(self.store.load_session())

    def test_main_never_prints_tokens_on_success_or_failure(self):
        host = self.prepared_host()
        supplied = json.dumps(fake_session()).encode("utf-8")
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(vm.main(["validate-stdin"], store=self.store,
                                     input_stream=BytesIO(supplied)), 0)
            self.assertEqual(vm.main(["import-stdin", "--apply", "--confirm-vm-host", host],
                                     store=self.store, input_stream=BytesIO(supplied)), 0)
            self.assertEqual(vm.main(["import-stdin", "--apply", "--confirm-vm-host", host],
                                     store=self.store, input_stream=BytesIO(supplied)), 1)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("SECRET_ACCESS_SENTINEL", output)
        self.assertNotIn("SECRET_REFRESH_SENTINEL", output)
        self.assertIn("DRY-RUN PASS", output)
        self.assertIn("IMPORT COMPLETE", output)

    def test_no_host_mutation_when_payload_invalid(self):
        host = self.prepared_host()
        bad = fake_session()
        bad["access_token"] = ""
        with self.assertRaises(vm.VMImportError):
            vm.import_session(self.store, bad, confirmed_host=host)
        self.assertEqual(vm.existing_vm_host(self.store), host)
        self.assertIsNone(self.store.load_session())

    def test_blocked_lock_does_not_import(self):
        host = self.prepared_host()
        with patch.object(vm, "_session_lock", side_effect=vm.SessionRefreshError("busy")):
            with self.assertRaises(vm.SessionRefreshError):
                vm.import_session(self.store, fake_session(), confirmed_host=host)
        self.assertIsNone(self.store.load_session())


if __name__ == "__main__":
    unittest.main()
