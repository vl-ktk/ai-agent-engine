"""Host adapter tests; all values and keys are synthetic and disposable."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_agent_engine.secure_vault import SecureVault, VaultCaller
from ai_agent_engine.vault_runtime import (
    OWNER_ID, PROJECT_ID, VaultProtocol, VaultUnavailable,
    _credential_file, backup_on_netcup,
)


@unittest.skipUnless(os.name == "posix", "POSIX host adapter")
class VaultRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vault-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.master = os.urandom(32)
        self.backup_key = os.urandom(32)
        self.vault = SecureVault(self.base / "state", owner_id=OWNER_ID,
                                 key_provider=lambda: self.master)
        self.addCleanup(self.vault.close)
        self.protocol = VaultProtocol(self.vault, root_uid=101, worker_uid=102)

    def request(self, uid: int, operation: str, **args: object) -> dict:
        result = self.protocol.process(uid, json.dumps(
            {"operation": operation, **args}).encode())
        return json.loads(result)

    def test_peer_os_uid_owner_and_reject_forged_principal(self) -> None:
        self.assertEqual(self.request(102, "status"), {"ok": True, "status": "ready"})
        self.assertEqual(self.request(100, "status")["error"], "denied")
        self.assertEqual(self.request(102, "ensure_project", project_id=PROJECT_ID)["error"], "denied")
        self.assertEqual(self.request(101, "ensure_project", project_id="other")["error"], "denied")
        self.assertEqual(self.request(101, "status", is_owner=True)["error"], "denied")
        self.assertEqual(self.protocol.process(101, b'{"operation":"status","operation":"get"}'),
                         b'{"ok":false,"error":"denied"}\n')

    def test_grant_only_approved_ops_revocation_and_no_echo(self) -> None:
        root = self.request(101, "ensure_project", project_id=PROJECT_ID)["category_id"]
        value = b"DELIBERATE-FAKE-SECRET-TEST-ONLY"
        encoded = base64.b64encode(value).decode()
        self.assertEqual(self.request(102, "save", category_id=root, label="fake",
                                      kind="api", value_b64=encoded)["error"], "denied")
        secret_id = self.request(101, "save", category_id=root, label="fake",
                                 kind="api", value_b64=encoded)["secret_id"]
        self.assertEqual(self.request(102, "get", secret_id=secret_id)["error"], "denied")
        self.assertTrue(self.request(101, "grant", secret_id=secret_id,
                                     operations=["get", "list"],
                                     expires_at=int(time.time()) + 60)["ok"])
        self.assertEqual(self.request(102, "get", secret_id=secret_id)["value_b64"], encoded)
        self.assertEqual(len(self.request(102, "list")["secrets"]), 1)
        self.assertEqual(self.request(102, "grant", secret_id=secret_id,
                                      operations=["get"], expires_at=int(time.time()) + 60)["error"], "denied")
        self.assertEqual(self.request(102, "update", secret_id=secret_id,
                                      value_b64=encoded)["error"], "denied")
        self.assertTrue(self.request(101, "revoke", secret_id=secret_id)["ok"])
        self.assertEqual(self.request(102, "get", secret_id=secret_id)["error"], "denied")
        self.assertNotIn(value, self.protocol.process(101, b'{"operation":"save","value_b64":"broken"}'))

    def test_credential_enforced_exact_size_and_no_symlink(self) -> None:
        directory = self.base / "credentials"
        directory.mkdir(mode=0o700)
        path = directory / "vault-master"
        path.write_bytes(self.master)
        path.chmod(0o600)
        self.assertEqual(_credential_file(directory, "vault-master"), self.master)
        path.chmod(0o604)
        with self.assertRaises(VaultUnavailable):
            _credential_file(directory, "vault-master")
        path.chmod(0o600)
        symlink = directory / "vault-backup"
        symlink.symlink_to(path)
        with self.assertRaises(VaultUnavailable):
            _credential_file(directory, "vault-backup")

    def test_manage_dynamic_categories_and_scoped_autosave(self) -> None:
        root = self.request(101, "ensure_project", project_id=PROJECT_ID)["category_id"]
        folder = self.request(101, "create_category", parent_id=root,
                              name="Servers")["category_id"]
        self.assertEqual(self.request(102, "list_categories")["error"], "denied")
        self.assertTrue(self.request(101, "rename_category", category_id=folder,
                                     name="Infrastructure")["ok"])
        self.assertTrue(any(x["name"] == "Infrastructure" for x in
                            self.request(101, "list_categories")["categories"]))
        self.assertTrue(self.request(101, "grant_category_save", category_id=folder,
                                     expires_at=int(time.time()) + 60)["ok"])
        sid = self.request(102, "save", category_id=folder, label="fictional",
                           kind="api", value_b64=base64.b64encode(b"fake-value").decode())["secret_id"]
        self.assertEqual(self.request(102, "get", secret_id=sid)["error"], "denied")
        self.assertTrue(self.request(101, "move_secret", secret_id=sid,
                                     category_id=root)["ok"])
        self.assertTrue(self.request(101, "revoke_category_save", category_id=folder)["ok"])
        self.assertEqual(self.request(102, "save", category_id=folder,
                                      label="next", kind="api",
                                      value_b64=base64.b64encode(b"fake").decode())["error"], "denied")

    def test_local_backup_retains_ciphertext_and_restores(self) -> None:
        owner = VaultCaller(OWNER_ID, PROJECT_ID, "owner-netcup", True)
        category = self.vault.ensure_project(owner, PROJECT_ID)
        value = b"FAKE-ONLY-PRIVATE-CONTENT-EXAMPLE"
        self.vault.save_secret(owner, category, "sample", "api", value)
        credentials = self.base / "credentials"
        credentials.mkdir(mode=0o700)
        for filename, content in (("vault-master", self.master),
                                  ("vault-backup", self.backup_key)):
            path = credentials / filename
            path.write_bytes(content)
            path.chmod(0o600)
        backup = self.base / "backups"
        backup.mkdir(mode=0o700)
        scratch = self.base / "runtime"
        scratch.mkdir(mode=0o700)
        env = {"CREDENTIALS_DIRECTORY": str(credentials),
               "STATE_DIRECTORY": str(self.base / "state"),
               "RUNTIME_DIRECTORY": str(scratch)}
        with patch.dict(os.environ, env):
            backup_on_netcup(backup_root=backup)
        files = list(backup.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn(value, files[0].read_bytes())
        self.assertNotIn(self.master, files[0].read_bytes())
        self.assertNotIn(self.backup_key, files[0].read_bytes())


if __name__ == "__main__":
    unittest.main()
