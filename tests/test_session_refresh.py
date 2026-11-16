"""Offline refresh rotation tests. No real OAuth request or credentials."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs

from ai_agent_engine.session_refresh import SessionRefreshError, refresh_if_needed
from ai_agent_engine.siwc import CredentialStore, TOKEN_URL, RESOURCE


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = CredentialStore(Path(self.scratch.name) / "private")
        self.store.save_session({
            "client_id": "oaiapp_owner",
            "subject": "owner",
            "access_token": "old-private-access",
            "refresh_token": "old-private-refresh",
            "sharing_enabled": True,
            "scopes": ["openid", "resource.invoke", "chatgpt.tokens.use.direct"],
            "expires_at": 50,
        })

    def test_near_expiry_refreshes_and_rotates_credentials(self):
        called = []
        def transport(request, timeout):
            called.append((request, timeout))
            return BytesIO(json.dumps({
                "access_token": "new-private-access",
                "refresh_token": "new-private-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            }).encode("utf-8"))

        self.assertTrue(refresh_if_needed(self.store, opener=transport,
                                          clock=lambda: 100))
        new = self.store.load_session()
        self.assertEqual(new["access_token"], "new-private-access")
        self.assertEqual(new["refresh_token"], "new-private-refresh")
        self.assertEqual(new["expires_at"], 3700)
        self.assertEqual(new["subject"], "owner")
        request, timeout = called[0]
        self.assertEqual(request.full_url, TOKEN_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 20)
        fields = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(fields["client_id"], ["oaiapp_owner"])
        self.assertEqual(fields["grant_type"], ["refresh_token"])
        self.assertEqual(fields["refresh_token"], ["old-private-refresh"])
        self.assertEqual(fields["resource"], [RESOURCE])
        self.assertNotIn("scope", fields)
        self.assertNotIn("old-private", repr(new))

    def test_valid_session_does_not_call_provider(self):
        session = self.store.load_session()
        session["expires_at"] = 200
        self.store.save_session(session)
        def never(request, timeout):
            raise AssertionError("Unexpected OAuth refresh")
        self.assertFalse(refresh_if_needed(self.store, opener=never,
                                           clock=lambda: 100))

    def test_rejects_scope_downgrade_without_overwriting_credentials(self):
        def transport(request, timeout):
            return BytesIO(json.dumps({
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": "openid profile",
            }).encode("utf-8"))
        with self.assertRaises(SessionRefreshError):
            refresh_if_needed(self.store, opener=transport, clock=lambda: 100)
        self.assertEqual(self.store.load_session()["refresh_token"], "old-private-refresh")

    def test_http_rejection_is_redacted_and_preserves_session(self):
        def transport(request, timeout):
            raise HTTPError(request.full_url, 401, "private error", None, None)
        with self.assertRaises(SessionRefreshError) as caught:
            refresh_if_needed(self.store, opener=transport, clock=lambda: 100)
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.store.load_session()["access_token"], "old-private-access")

    def test_rejects_incomplete_token_replacement(self):
        def transport(request, timeout):
            return BytesIO(json.dumps({
                "access_token": "new-access",
                "expires_in": 0,
            }).encode("utf-8"))
        with self.assertRaises(SessionRefreshError):
            refresh_if_needed(self.store, opener=transport, clock=lambda: 100)
        self.assertEqual(self.store.load_session()["access_token"], "old-private-access")


    def test_posix_lock_is_exclusive_bounded_and_released(self):
        from ai_agent_engine.session_refresh import _session_lock
        import os
        if os.name != "posix":
            self.skipTest("POSIX native lock is tested on Linux CI")
        with _session_lock(self.store):
            with self.assertRaisesRegex(SessionRefreshError, "Timed out"):
                with _session_lock(self.store, wait_seconds=0):
                    self.fail("A second process must not acquire the same lock")
        with _session_lock(self.store, wait_seconds=0):
            pass

    def test_lock_file_is_never_unlinked_and_is_mode_0600(self):
        from ai_agent_engine.session_refresh import _session_lock
        import os
        import stat
        if os.name != "posix":
            self.skipTest("File permission bits are POSIX-specific")
        with _session_lock(self.store):
            lock = self.store.root / ".refresh.lock"
            self.assertTrue(lock.exists())
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertTrue(lock.exists())

    def test_symlink_lock_is_rejected_without_touching_target(self):
        from ai_agent_engine.session_refresh import _session_lock
        import os
        if os.name != "posix":
            self.skipTest("Symlink test requires POSIX filesystem behavior")
        self.store._ensure_root()
        target = self.store.root / "private-target"
        target.write_text("unchanged", encoding="utf-8")
        (self.store.root / ".refresh.lock").symlink_to(target)
        with self.assertRaisesRegex(SessionRefreshError, "symlink"):
            with _session_lock(self.store):
                self.fail("Must never lock a symlink")
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_windows_lock_uses_msvcrt_single_byte_and_releases(self):
        from ai_agent_engine import session_refresh
        import os
        import sys
        from types import ModuleType
        from unittest.mock import patch
        fake = ModuleType("msvcrt")
        fake.LK_NBLCK = 2
        fake.LK_UNLCK = 0
        seen = []

        def lock(descriptor, mode, count):
            seen.append((mode, count, os.lseek(descriptor, 0, os.SEEK_CUR)))
        fake.locking = lock
        with patch.dict(sys.modules, {"msvcrt": fake}):
            with patch.object(session_refresh.os, "name", "nt"):
                with session_refresh._session_lock(self.store):
                    self.assertEqual(seen, [(fake.LK_NBLCK, 1, 0)])
        self.assertEqual(seen[-1], (fake.LK_UNLCK, 1, 0))

    def test_windows_lock_contention_retries_then_succeeds(self):
        from ai_agent_engine import session_refresh
        import errno
        import sys
        from types import ModuleType
        from unittest.mock import patch
        fake = ModuleType("msvcrt")
        fake.LK_NBLCK = 2
        fake.LK_UNLCK = 0
        attempts = []

        def lock(descriptor, mode, count):
            attempts.append(mode)
            if mode == fake.LK_NBLCK and attempts.count(fake.LK_NBLCK) == 1:
                raise OSError(errno.EACCES, "busy (synthetic)")
        fake.locking = lock
        with patch.dict(sys.modules, {"msvcrt": fake}):
            with patch.object(session_refresh.os, "name", "nt"):
                with patch.object(session_refresh.time, "sleep", return_value=None):
                    with session_refresh._session_lock(self.store):
                        pass
        self.assertEqual(attempts, [fake.LK_NBLCK, fake.LK_NBLCK, fake.LK_UNLCK])

    def test_refresh_lock_hard_failure_does_not_change_saved_tokens(self):
        from ai_agent_engine import session_refresh
        from unittest.mock import patch
        original = self.store.load_session()
        with patch.object(session_refresh, "_session_lock",
                          side_effect=SessionRefreshError("synthetic lock refusal")):
            with self.assertRaises(SessionRefreshError):
                refresh_if_needed(self.store, clock=lambda: 100)
        self.assertEqual(original, self.store.load_session())



    def test_native_interprocess_lock_blocks_second_python_process(self):
        """Unlike mocked msvcrt tests, exercises the actual OS file lock.

        Uses fabricated credentials only, and never invokes token endpoints.
        Runs unmodified on the Windows runner and on the Linux runners.
        """
        from ai_agent_engine.session_refresh import _session_lock
        import os
        import subprocess
        import sys

        if os.name not in ("posix", "nt"):
            self.skipTest("Native interprocess lock is unavailable")

        child_script = """
import sys
from pathlib import Path
from ai_agent_engine.session_refresh import _session_lock, SessionRefreshError
from ai_agent_engine.siwc import CredentialStore

try:
    with _session_lock(CredentialStore(Path(sys.argv[1])), wait_seconds=0.25):
        print("ACQUIRED")
except SessionRefreshError:
    print("BLOCKED")
"""
        def call_child():
            result = subprocess.run(
                [sys.executable, "-c", child_script, str(self.store.root)],
                capture_output=True, text=True, timeout=12, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        lock_path = self.store.root / ".refresh.lock"
        with _session_lock(self.store):
            self.assertTrue(lock_path.is_file())
            self.assertEqual(call_child(), "BLOCKED")
        # Must unlock without removing/replacing the persistent lock file.
        self.assertTrue(lock_path.is_file())
        self.assertEqual(call_child(), "ACQUIRED")

    def test_native_host_id_persists_across_python_process(self):
        """A future VM cannot silently acquire a second host ID on reboot."""
        import subprocess
        import sys

        host = self.store.host_id()
        script = ("import sys; from pathlib import Path; "
                  "from ai_agent_engine.siwc import CredentialStore; "
                  "print(CredentialStore(Path(sys.argv[1])).host_id())")
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.store.root)],
            capture_output=True, text=True, timeout=12, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), host)


if __name__ == "__main__":
    unittest.main()
