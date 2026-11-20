"""Private Unix client tests with synthetic data only; no network or keys."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_agent_engine.secure_vault import SecureVault, VaultDenied, VaultUnavailable
from ai_agent_engine.vault_client import VaultUnixClient, VaultOrchestratorTasks
from ai_agent_engine import vault_client as client_module
from ai_agent_engine.vault_runtime import VaultProtocol, VaultUnixServer, PROJECT_ID, OWNER_ID


@unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux peer credentials")
class VaultClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="vault-client-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.master = os.urandom(32)
        self.vault = SecureVault(self.root / "db", owner_id=OWNER_ID,
                                 key_provider=lambda: self.master)
        self.addCleanup(self.vault.close)
        self.peer_uid = os.geteuid()
        self.server_protocol = VaultProtocol(
            self.vault, root_uid=self.peer_uid, worker_uid=self.peer_uid + 1)
        self.socket_path = self.root / "vault.sock"

    def owner_request(self, operation: str, **fields: object) -> dict:
        data = json.dumps({"operation": operation, **fields}).encode()
        return json.loads(self.server_protocol.process(self.peer_uid, data))

    def test_live_private_socket_roundtrip_and_reject_empty_grant(self) -> None:
        root = self.owner_request("ensure_project", project_id=PROJECT_ID)["category_id"]
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(self.socket_path))
        except OSError as exc:
            self.skipTest(f"sandbox lacks local AF_UNIX support: errno {exc.errno}")
        sock.listen()
        self.socket_path.chmod(0o660)
        server = VaultUnixServer(sock, self.server_protocol)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)

        client = VaultUnixClient(socket_path=self.socket_path,
                                 expected_service_uid=self.peer_uid)
        bridge = VaultOrchestratorTasks(client)
        self.assertTrue(client.status())
        fake = b"DEMONSTRATION-ONLY-NOT-A-PASSWORD"
        secret_id = bridge.save_supplied_secret(category_id=root, label="test-only",
                                                 kind="api", value=fake)
        self.assertTrue(secret_id.startswith("sec_"))
        self.assertEqual(client.get_secret(secret_id), fake)
        self.assertEqual(client.list_metadata()[0]["label"], "test-only")
        self.assertTrue(bridge.use_approved_secret(secret_id, lambda v: v == fake))
        with self.assertRaises(VaultDenied):
            bridge.use_approved_secret(secret_id, lambda v: v)
        self.assertEqual(client.update_secret(secret_id, b"NEXT-FAKE-VALUE"), 2)
        self.assertEqual(client.get_secret(secret_id), b"NEXT-FAKE-VALUE")
        self.assertNotIn(fake, (self.root / "db/vault.sqlite3").read_bytes())

        # Actual Linux uid never comes from an incoming JSON is_owner field.
        forged = self.server_protocol.process(self.peer_uid + 1,
                                              b'{"operation":"status","is_owner":true}')
        self.assertEqual(forged, b'{"ok":false,"error":"denied"}\n')
        worker_view = self.server_protocol.process(self.peer_uid + 1,
                                json.dumps({"operation": "get", "secret_id": secret_id}).encode())
        self.assertEqual(worker_view, b'{"ok":false,"error":"denied"}\n')

    def test_mismatched_uid_and_non_socket_path_fail_closed(self) -> None:
        bogus = self.root / "vault.sock"
        bogus.write_text("fake-file", encoding="utf-8")
        bogus.chmod(0o660)
        client = VaultUnixClient(socket_path=bogus,
                                 expected_service_uid=os.geteuid())
        with self.assertRaises(VaultUnavailable):
            client.status()
        bogus.unlink()
        bogus.symlink_to(self.root / "untrusted.sock")
        with self.assertRaises(VaultUnavailable):
            VaultUnixClient(socket_path=bogus, expected_service_uid=os.geteuid())

    def test_full_roundtrip_over_synthetic_kernel_verified_transport(self) -> None:
        root = self.owner_request("ensure_project", project_id=PROJECT_ID)["category_id"]
        uid = self.peer_uid
        protocol = self.server_protocol

        class LocalConn:
            def __init__(self):
                self.response = b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def settimeout(self, seconds):
                self.timeout = seconds

            def connect(self, path):
                if path != str(self_path):
                    raise OSError("bad connection")

            def getsockopt(self, level, kind, size):
                return struct.pack("3i", 123, uid, uid)

            def sendall(self, data):
                self.response = protocol.process(uid, data.strip())

            def recv(self, count):
                part, self.response = self.response[:count], self.response[count:]
                return part

        self_path = self.socket_path
        sock_meta = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=uid)
        client = VaultUnixClient(socket_path=self_path, expected_service_uid=uid)
        with (patch.object(client_module.os, "lstat", return_value=sock_meta),
              patch.object(client_module.socket, "socket", side_effect=lambda *args: LocalConn())):
            self.assertTrue(client.status())
            fake = b"DEMONSTRATION-NOT-A-REAL-TOKEN"
            sid = client.save_secret(category_id=root, label="synthetic", kind="api", value=fake)
            self.assertEqual(client.get_secret(sid), fake)
            self.assertEqual(client.list_metadata()[0]["id"], sid)
            self.assertEqual(client.update_secret(sid, b"UPDATED-ONLY-FAKE"), 2)
            self.assertEqual(client.get_secret(sid), b"UPDATED-ONLY-FAKE")
        self.assertNotIn(fake, (self.root / "db/vault.sqlite3").read_bytes())

    def test_systemd_pid1_listener_accepted_but_other_root_rejected(self) -> None:
        service_uid = 12121
        sock_meta = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660,
                                    st_uid=service_uid)
        client = VaultUnixClient(socket_path=self.socket_path,
                                 expected_service_uid=service_uid)

        class Connection:
            def __init__(self, pid: int):
                self.pid = pid
                self.reply = b'{"ok":true,"status":"ready"}\n'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def settimeout(self, timeout):
                return None

            def connect(self, path):
                return None

            def getsockopt(self, level, option, size):
                return struct.pack("3i", self.pid, 0, 0)

            def sendall(self, data):
                return None

            def recv(self, size):
                reply, self.reply = self.reply, b""
                return reply

        with (patch.object(client_module.os, "lstat", return_value=sock_meta),
              patch.object(client_module.socket, "socket", side_effect=lambda *args: Connection(1))):
            self.assertTrue(client.status())
        with (patch.object(client_module.os, "lstat", return_value=sock_meta),
              patch.object(client_module.socket, "socket", side_effect=lambda *args: Connection(999))):
            with self.assertRaises(VaultUnavailable):
                client.status()

    def test_deny_invalid_secret_and_sanitize_transport_failure(self) -> None:
        client = VaultUnixClient(socket_path=self.socket_path,
                                 expected_service_uid=os.geteuid())
        with self.assertRaises(VaultDenied):
            client.save_secret(category_id="cat_fake", label="x", kind="api", value=b"")
        with self.assertRaises(VaultUnavailable):
            client.get_secret("sec_absent")

    def test_bad_response_values_rejected_without_echoing(self) -> None:
        client = VaultUnixClient(socket_path=self.socket_path,
                                 expected_service_uid=os.geteuid())
        with patch.object(client, "_send", return_value={"ok": True, "value_b64": "not base64"}):
            with self.assertRaises(VaultUnavailable):
                client.get_secret("sec_demo")
        with patch.object(client, "_send", return_value={"ok": True, "secrets": [
                {"id": "sec_demo", "label": "test", "kind": "api",
                 "revision": 1, "value_b64": "leak"}]}):
            with self.assertRaises(VaultUnavailable):
                client.list_metadata()


if __name__ == "__main__":
    unittest.main()
