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


if __name__ == "__main__":
    unittest.main()
