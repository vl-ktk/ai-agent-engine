"""Trusted orchestrator-to-Vault wiring with fictional credentials only."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_agent_engine.main_orchestrator import (
    InvalidOrchestratorRequest, MainOrchestratorVault,
)
from ai_agent_engine.secure_vault import VaultDenied
from ai_agent_engine.task_api import OwnerScope
from ai_agent_engine.vault_client import VaultUnixClient


@unittest.skipUnless(os.name == "posix", "Netcup internal transport requires POSIX")
class MainOrchestratorVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = OwnerScope("vl-ktk", frozenset({"ai-agent-engine"}))
        self.client = VaultUnixClient(socket_path=Path("/run/ae-vault.sock"),
                                      expected_service_uid=12345)
        self.main = MainOrchestratorVault(self.scope, self.client)

    def test_owner_chat_save_uses_local_vault_without_extra_confirmation(self) -> None:
        fictional = b"SYNTHETIC-NOT-A-REAL-PASSWORD"
        with patch.object(self.client, "_send", return_value={
                "ok": True, "secret_id": "sec_synthetic"}) as send:
            sid = self.main.save_from_authenticated_owner(
                category_id="cat_approved", label="Oracle-1",
                kind="password", value=fictional)
        self.assertEqual(sid, "sec_synthetic")
        args, fields = send.call_args
        self.assertEqual(args, ("save",))
        self.assertEqual(fields["category_id"], "cat_approved")
        self.assertEqual(base64.b64decode(fields["value_b64"]), fictional)
        self.assertNotIn("is_owner", fields)
        self.assertNotIn(fictional.decode(), sid)

    def test_use_secret_returns_only_boolean_not_payload(self) -> None:
        fictional = b"SYNTHETIC-NOT-REAL-TOKEN"
        with patch.object(self.client, "_send", return_value={
                "ok": True, "value_b64": base64.b64encode(fictional).decode()}):
            result = self.main.use_for_approved_task(
                "sec_demo", lambda value: value == fictional)
            self.assertIs(result, True)
            with self.assertRaises(VaultDenied):
                self.main.use_for_approved_task("sec_demo", lambda value: value)

    def test_foreign_owner_or_project_and_fake_client_denied(self) -> None:
        for scope in (
                OwnerScope("someoneelse", frozenset({"ai-agent-engine"})),
                OwnerScope("vl-ktk", frozenset({"ktk-social"}))):
            with self.subTest(scope=scope):
                with self.assertRaises(InvalidOrchestratorRequest):
                    MainOrchestratorVault(scope, self.client)
        with self.assertRaises(InvalidOrchestratorRequest):
            MainOrchestratorVault(self.scope, object())


if __name__ == "__main__":
    unittest.main()
