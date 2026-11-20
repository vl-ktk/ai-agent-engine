"""7.7A offline tests with synthetic secrets, private disposable databases."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from ai_agent_engine.secure_vault import (
    SecureVault, VaultCaller, VaultDenied, VaultOrchestratorBridge,
    VaultUnavailable,
)


@unittest.skipUnless(os.name == "posix", "private filesystem modes require POSIX")
class SecureVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-vault-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        self.key = os.urandom(32)
        self.backup_key = os.urandom(32)
        self.owner = VaultCaller("owner", "engine", "owner-session", True)
        self.worker = VaultCaller("owner", "engine", "codex")
        self.other = VaultCaller("owner", "social", "claude")
        self.foreign = VaultCaller("foreign", "engine", "owner-session", True)
        self.vault = SecureVault(self.root, owner_id="owner", key_provider=lambda: self.key)
        self.addCleanup(self.vault.close)
        self.engine_root = self.vault.ensure_project(self.owner, "engine")
        self.vault.ensure_project(self.owner, "social")
        self.sentinel = b"SYNTHETIC-VALUE-VERY-PRIVATE-7-7A"

    def save(self) -> str:
        return self.vault.save_secret(self.owner, self.engine_root,
                                      "Oracle-1", "password", self.sentinel)

    def test_save_get_update_and_list_without_plaintext_on_disk(self) -> None:
        sid = self.save()
        self.assertEqual(self.vault.get_secret(self.owner, sid), self.sentinel)
        entries = self.vault.list_secrets(self.owner)
        self.assertEqual([(x.label, x.revision) for x in entries], [("Oracle-1", 1)])
        self.assertNotIn(self.sentinel, self.root.joinpath("vault.sqlite3").read_bytes())
        self.assertNotIn(self.sentinel, repr(self.vault.audit_events(self.owner)).encode())
        self.assertEqual(self.vault.update_secret(self.owner, sid, b"next-synthetic"), 2)
        self.assertEqual(self.vault.get_secret(self.owner, sid), b"next-synthetic")

    def test_explicit_grant_and_cross_project_share(self) -> None:
        sid = self.save()
        with self.assertRaises(VaultDenied):
            self.vault.get_secret(self.worker, sid)
        with self.assertRaises(VaultDenied):
            self.vault.get_secret(self.other, sid)
        with self.assertRaises(VaultDenied):
            self.vault.get_secret(self.foreign, sid)
        exp = int(time.time()) + 60
        self.vault.grant(self.owner, sid, "codex", "engine",
                         frozenset({"get", "list"}), exp)
        self.assertEqual(self.vault.get_secret(self.worker, sid), self.sentinel)
        self.assertEqual(len(self.vault.list_secrets(self.worker)), 1)
        self.assertEqual(self.vault.list_secrets(self.other), ())
        self.vault.grant(self.owner, sid, "claude", "social",
                         frozenset({"get"}), exp)
        self.assertEqual(self.vault.get_secret(self.other, sid), self.sentinel)
        self.assertEqual(len(self.vault.list_secrets(self.other)), 0)
        self.assertEqual(len(self.vault.list_secrets(self.owner)), 1)
        with self.assertRaises(VaultDenied):
            self.vault.update_secret(self.worker, sid, b"altered")

    def test_revoke_blocks_immediately_and_expiry_blocks(self) -> None:
        sid = self.save()
        exp = int(time.time()) + 60
        self.vault.grant(self.owner, sid, "codex", "engine", frozenset({"get"}), exp)
        self.vault.revoke(self.owner, sid, "codex", "engine")
        with self.assertRaises(VaultDenied):
            self.vault.get_secret(self.worker, sid)
        with self.assertRaises(VaultDenied):
            self.vault.grant(self.owner, sid, "codex", "engine",
                             frozenset({"get"}), int(time.time()) - 1)

    def test_non_owner_cannot_self_grant_and_invalid_scope(self) -> None:
        sid = self.save()
        with self.assertRaises(VaultDenied):
            self.vault.grant(self.worker, sid, "codex", "engine",
                             frozenset({"get"}), int(time.time()) + 30)
        with self.assertRaises(VaultDenied):
            self.vault.ensure_project(self.worker, "stolen")
        with self.assertRaises(VaultDenied):
            self.vault.save_secret(self.foreign, self.engine_root,
                                   "x", "password", self.sentinel)
        with self.assertRaises(VaultDenied):
            self.vault.grant(self.owner, sid, "codex", "unapproved",
                             frozenset({"get"}), int(time.time()) + 30)

    def test_dynamic_categories_moving_and_cycle_denial(self) -> None:
        a = self.vault.create_category(self.owner, self.engine_root, "Servers")
        b = self.vault.create_category(self.owner, a, "Oracle")
        c = self.vault.create_category(self.owner, self.engine_root, "Services")
        self.vault.rename_category(self.owner, a, "Hosts")
        self.vault.move_category(self.owner, b, c)
        with self.assertRaises(VaultDenied):
            self.vault.move_category(self.owner, c, c)
        with self.assertRaises(VaultDenied):
            self.vault.move_category(self.owner, self.engine_root, c)
        with self.assertRaises(VaultDenied):
            self.vault.move_category(self.owner, c, b)
        sid = self.vault.save_secret(self.owner, b, "Oracle-2", "ssh", self.sentinel)
        self.assertEqual(self.vault.get_secret(self.owner, sid), self.sentinel)
        self.vault.move_secret(self.owner, sid, a)
        self.assertEqual(self.vault.get_secret(self.owner, sid), self.sentinel)
        self.assertEqual(self.vault.list_secrets(self.owner)[0].category_id, a)

    def test_category_archive_blocks_nested_use_and_restores(self) -> None:
        a = self.vault.create_category(self.owner, self.engine_root, "Servers")
        b = self.vault.create_category(self.owner, a, "Netcup")
        sid = self.vault.save_secret(self.owner, b, "demo", "api", self.sentinel)
        self.vault.archive_category(self.owner, a)
        with self.assertRaises(VaultDenied):
            self.vault.get_secret(self.owner, sid)
        with self.assertRaises(VaultDenied):
            self.vault.save_secret(self.owner, b, "new", "api", self.sentinel)
        self.assertEqual(self.vault.list_secrets(self.owner), ())
        self.vault.restore_category(self.owner, a)
        self.assertEqual(self.vault.get_secret(self.owner, sid), self.sentinel)
        self.assertTrue(any(row[0] == b for row in self.vault.list_categories(self.owner)))

    def test_executor_auto_saves_created_key_with_existing_category_grant(self) -> None:
        with self.assertRaises(VaultDenied):
            self.vault.save_secret(self.worker, self.engine_root,
                                   "new-service-key", "api", self.sentinel)
        self.vault.grant_category_save(self.owner, self.engine_root,
                                       "codex", int(time.time()) + 60)
        sid = self.vault.save_secret(self.worker, self.engine_root,
                                     "new-service-key", "api", self.sentinel)
        with self.assertRaises(VaultDenied):
            self.vault.get_secret(self.worker, sid)
        self.assertEqual(self.vault.get_secret(self.owner, sid), self.sentinel)
        self.vault.revoke_category_save(self.owner, self.engine_root, "codex")
        with self.assertRaises(VaultDenied):
            self.vault.save_secret(self.worker, self.engine_root,
                                   "second-key", "api", self.sentinel)

    def test_reversible_archive_does_not_destroy_ciphertext(self) -> None:
        sid = self.save()
        self.vault.archive_secret(self.owner, sid)
        with self.assertRaises(VaultDenied):
            self.vault.get_secret(self.owner, sid)
        self.assertEqual(self.vault.list_secrets(self.owner), ())
        self.vault.restore_secret(self.owner, sid)
        self.assertEqual(self.vault.get_secret(self.owner, sid), self.sentinel)

    def test_owner_chat_bridge_no_extra_consent(self) -> None:
        bridge = VaultOrchestratorBridge(self.vault)
        sid = bridge.save_from_owner_message(self.owner, self.engine_root,
                                             "Oracle-1", "password", self.sentinel)
        self.assertTrue(sid.startswith("sec_"))
        result = bridge.use_for_approved_task(self.owner, sid,
                                               lambda v: v == self.sentinel)
        self.assertTrue(result)
        with self.assertRaises(VaultDenied):
            bridge.use_for_approved_task(self.owner, sid, lambda v: v)
        with self.assertRaises(VaultDenied):
            bridge.save_from_owner_message(self.worker, self.engine_root,
                                           "Oracle-1", "password", self.sentinel)

    def test_restart_with_external_same_key_and_wrong_key(self) -> None:
        sid = self.save()
        self.vault.close()
        restarted = SecureVault(self.root, owner_id="owner", key_provider=lambda: self.key)
        self.assertEqual(restarted.get_secret(self.owner, sid), self.sentinel)
        restarted.close()
        wrong = SecureVault(self.root, owner_id="owner", key_provider=lambda: os.urandom(32))
        with self.assertRaisesRegex(VaultUnavailable, "Vault unavailable") as error:
            wrong.get_secret(self.owner, sid)
        self.assertNotIn(self.sentinel.decode(), str(error.exception))
        wrong.close()

    def test_bad_key_provider_diagnostics_not_exposed(self) -> None:
        sid = self.save()
        self.vault.close()

        def unavailable_key() -> bytes:
            raise RuntimeError("SYNTHETIC-PRIVATE-KEY-PROVIDER-DETAIL")

        wrong = SecureVault(self.root, owner_id="owner",
                            key_provider=unavailable_key)
        try:
            with self.assertRaises(VaultUnavailable) as error:
                wrong.get_secret(self.owner, sid)
            self.assertNotIn("PRIVATE-KEY", str(error.exception))
        finally:
            wrong.close()

    def test_encrypted_backup_restores_isolated_directory(self) -> None:
        sid = self.save()
        encrypted = self.vault.encrypted_backup(self.owner, self.backup_key)
        self.assertNotIn(self.sentinel, encrypted)
        destination = Path(self.tmp.name) / "offline-restored"
        restored = SecureVault.restore_backup(
            encrypted, self.backup_key, destination, owner_id="owner",
            key_provider=lambda: self.key)
        try:
            self.assertEqual(restored.get_secret(self.owner, sid), self.sentinel)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual((destination / "vault.sqlite3").stat().st_mode & 0o777, 0o600)
        finally:
            restored.close()
        with self.assertRaises(VaultUnavailable):
            SecureVault.restore_backup(encrypted, self.backup_key, destination,
                                       owner_id="owner", key_provider=lambda: self.key)

    def test_encrypted_backup_tamper_and_foreign_owner_denied(self) -> None:
        self.save()
        blob = self.vault.encrypted_backup(self.owner, self.backup_key)
        with self.assertRaises(VaultUnavailable):
            SecureVault.restore_backup(blob[:-1] + bytes([blob[-1] ^ 1]),
                                       self.backup_key, Path(self.tmp.name) / "bad",
                                       owner_id="owner", key_provider=lambda: self.key)
        with self.assertRaises(VaultUnavailable):
            SecureVault.restore_backup(blob, self.backup_key,
                                       Path(self.tmp.name) / "other",
                                       owner_id="other", key_provider=lambda: self.key)

    def test_db_ciphertext_tamper_fails_closed(self) -> None:
        sid = self.save()
        with self.vault._lock, self.vault._transaction():
            self.vault._db.execute("UPDATE secrets SET ciphertext=?"
                                   " WHERE secret_id=?", (os.urandom(35), sid))
        with self.assertRaises(VaultUnavailable):
            self.vault.get_secret(self.owner, sid)

    def test_no_symlink_or_permissive_directory(self) -> None:
        destination = Path(self.tmp.name) / "root-link"
        destination.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(VaultUnavailable):
            SecureVault(destination, owner_id="owner", key_provider=lambda: self.key)
        self.root.chmod(0o777)
        with self.assertRaises(VaultUnavailable):
            SecureVault(self.root, owner_id="owner", key_provider=lambda: self.key)

    def test_sql_audit_is_append_only(self) -> None:
        sid = self.save()
        with self.assertRaises(sqlite3.DatabaseError):
            with self.vault._lock, self.vault._transaction():
                self.vault._db.execute("DELETE FROM audit")
        self.assertTrue(any(row[3] == "save" and row[4] == sid
                            for row in self.vault.audit_events(self.owner)))


if __name__ == "__main__":
    unittest.main()
