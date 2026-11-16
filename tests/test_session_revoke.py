"""Offline sign-out and revocation tests; never contact OpenAI."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from ai_agent_engine.session_revoke import DISCOVERY_URL, sign_out
from ai_agent_engine.siwc import CredentialStore


class FakeResponse(BytesIO):
    def __init__(self, data=b"", status=200):
        super().__init__(data)
        self.status = status


class SignOutTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CredentialStore(Path(directory.name) / "secrets")
        self.store.host_id()
        self.store.save_session({
            "client_id": "oaiapp_owner", "subject": "owner",
            "access_token": "private-access", "refresh_token": "private-refresh",
            "id_token": "private-id",
        })
        self.original_host = self.store.host_id()

    @staticmethod
    def discover(endpoint="https://auth.openai.com/oauth/revoke"):
        return FakeResponse(json.dumps({
            "issuer": "https://auth.openai.com",
            "revocation_endpoint": endpoint,
        }).encode("utf-8"))

    def test_success_revokes_refresh_and_retains_registration_only(self):
        seen = []
        def transport(request, timeout):
            seen.append((request, timeout))
            if request.full_url == DISCOVERY_URL:
                self.assertEqual(request.get_method(), "GET")
                return self.discover()
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, "https://auth.openai.com/oauth/revoke")
            fields = parse_qs(request.data.decode("utf-8"))
            self.assertEqual(fields["token"], ["private-refresh"])
            self.assertEqual(fields["token_type_hint"], ["refresh_token"])
            self.assertEqual(fields["client_id"], ["oaiapp_owner"])
            return FakeResponse()

        self.assertTrue(sign_out(self.store, opener=transport, sleep=lambda _: None))
        self.assertEqual(len(seen), 2)
        self.assertIsNone(self.store.load_session())
        self.assertEqual(self.store.client_id(), "oaiapp_owner")
        self.assertEqual(self.store.host_id(), self.original_host)
        self.assertNotIn("private-refresh",
                         (self.store.root / "pending-registration.json").read_text())

    def test_unreachable_provider_clears_local_session_without_leaking_token(self):
        def transport(request, timeout):
            raise URLError("private-access PRIVATE refresh endpoint")
        self.assertFalse(sign_out(self.store, opener=transport, sleep=lambda _: None))
        self.assertIsNone(self.store.load_session())
        self.assertEqual(self.store.client_id(), "oaiapp_owner")

    def test_retries_server_error_with_backoff(self):
        calls = []
        delays = []
        def transport(request, timeout):
            if request.full_url == DISCOVERY_URL:
                return self.discover()
            calls.append(request.full_url)
            if len(calls) <= 2:
                raise HTTPError(request.full_url, 503, "private token", None, None)
            return FakeResponse()
        self.assertTrue(sign_out(self.store, opener=transport, sleep=delays.append))
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [0.25, 0.5])

    def test_does_not_send_token_to_untrusted_revocation_endpoint(self):
        calls = []
        def transport(request, timeout):
            calls.append(request.full_url)
            return self.discover("https://evil.example/revoke")
        self.assertFalse(sign_out(self.store, opener=transport, sleep=lambda _: None))
        self.assertEqual(calls, [DISCOVERY_URL])
        self.assertIsNone(self.store.load_session())

    def test_invalid_client_or_refresh_does_not_send_network_request(self):
        session = self.store.load_session()
        session["refresh_token"] = ""
        self.store.save_session(session)
        def unexpected(request, timeout):
            raise AssertionError("Network should not be used without a valid token")
        self.assertFalse(sign_out(self.store, opener=unexpected))
        self.assertIsNone(self.store.load_session())
        self.assertEqual(self.store.client_id(), "oaiapp_owner")

    def test_untouched_when_nothing_is_signed_in(self):
        self.store.clear_session()
        def unexpected(request, timeout):
            raise AssertionError("Network should not be used without a session")
        self.assertTrue(sign_out(self.store, opener=unexpected))
        self.assertEqual(self.store.client_id(), "oaiapp_owner")


if __name__ == "__main__":
    unittest.main()
