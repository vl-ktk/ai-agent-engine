"""Local-only SIWC authorization tests. No real login or HTTP API calls."""

from contextlib import redirect_stdout
import hashlib
from http.client import HTTPConnection
from io import StringIO
import importlib.util
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ai_agent_engine import siwc


class AuthorizationTests(unittest.TestCase):
    def test_pkce_uses_fresh_verifier_and_correct_challenge(self):
        verifier, challenge = siwc.generate_pkce()
        self.assertNotEqual(verifier, siwc.generate_pkce()[0])
        self.assertEqual(challenge, siwc._base64url(hashlib.sha256(verifier.encode()).digest()))

    def test_initial_registration_uses_exact_parameters(self):
        url = siwc.authorization_url(
            redirect_uri="http://127.0.0.1:1455/auth/callback",
            state="safe-state", nonce="safe-nonce", code_challenge="a-pkce-challenge",
            host_id="urn:uuid:123e4567-e89b-42d3-a456-426614174000",
        )
        self.assertTrue(url.startswith(siwc.AUTHORIZE_URL + "?"))
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["client_id"], ["dynamic_agent_client"])
        self.assertEqual(query["agent_name_hint"], ["AI Agent Engine"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:1455/auth/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["resource"], [siwc.RESOURCE])
        self.assertIn(siwc.DIRECT_SCOPE, query["scope"][0].split())

    def test_reauthorization_reuses_issued_client_without_name_hint(self):
        url = siwc.authorization_url(
            redirect_uri="http://127.0.0.1:9999/auth/callback",
            state="s", nonce="n", code_challenge="c", host_id="urn:uuid:test",
            issued_client_id="oaiapp_existing",
        )
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["client_id"], ["oaiapp_existing"])
        self.assertNotIn("agent_name_hint", query)

    def test_loopback_listener_validates_state_and_does_not_echo_code(self):
        with siwc._CallbackServer("expected-state") as server:
            port = server.server_port
            connection = HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/auth/callback?state=wrong&code=private-code")
            # Process requests explicitly; no background thread or external network.
            server.handle_request()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertNotIn(b"private-code", response.read())
            self.assertIsNone(server.result)
            connection.close()

            connection = HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/auth/callback?state=expected-state&code=private-code&client_id=oaiapp_test")
            server.handle_request()
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertNotIn(b"private-code", response.read())
            self.assertEqual(server.result["code"], "private-code")
            connection.close()

    def test_host_and_registration_reused_and_secrets_private(self):
        with TemporaryDirectory() as directory:
            store = siwc.CredentialStore(Path(directory) / "tokens")
            host = store.host_id()
            self.assertEqual(store.host_id(), host)
            self.assertTrue(host.startswith("urn:uuid:"))
            store.remember_registration("oaiapp_issued")
            self.assertEqual(store.client_id(), "oaiapp_issued")
            with self.assertRaises(siwc.AuthError):
                store.remember_registration("oaiapp_different")
            data = {
                "client_id": "oaiapp_issued", "subject": "user1",
                "access_token": "never-print-this-token",
                "refresh_token": "never-print-this-refresh",
            }
            store.save_session(data)
            self.assertIsNone(store._read("pending-registration.json"))
            self.assertEqual(store.client_id(), "oaiapp_issued")
            with self.assertRaises(siwc.AuthError):
                store.save_session({**data, "subject": "other-user"})
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE((store.root / "session.json").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)


@unittest.skipUnless(importlib.util.find_spec("jwt"), "Optional PyJWT dependency not installed")
class TokenValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def _token(self, **overrides):
        import jwt
        claims = {
            "iss": siwc.ISSUER, "sub": "user123", "aud": "oaiapp_test",
            "nonce": "nonce123", "iat": int(time.time()),
            "exp": int(time.time()) + 1800, "email": "user@example.test",
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def test_verified_token_and_granted_scope_required(self):
        token = self._token()
        credentials = {
            "id_token": token, "access_token": "dummy-access",
            "refresh_token": "dummy-refresh", "token_type": "Bearer",
            "scope": "openid offline_access resource.invoke chatgpt.tokens.use.direct",
            "expires_in": 3600,
        }
        session = siwc.validated_session(credentials, client_id="oaiapp_test",
                                         host_id="urn:uuid:test", nonce="nonce123",
                                         signing_key=self.public_key)
        self.assertTrue(session["sharing_enabled"])
        self.assertEqual(session["subject"], "user123")
        without_permission = siwc.validated_session(
            {**credentials, "scope": "openid profile"}, client_id="oaiapp_test",
            host_id="urn:uuid:test", nonce="nonce123", signing_key=self.public_key,
        )
        self.assertFalse(without_permission["sharing_enabled"])

    def test_rejects_wrong_nonce_audience_issuer_and_expired_signature(self):
        cases = [
            (self._token(nonce="evil"), "nonce123"),
            (self._token(aud="other"), "nonce123"),
            (self._token(iss="https://other.example"), "nonce123"),
            (self._token(exp=int(time.time()) - 100), "nonce123"),
        ]
        for token, nonce in cases:
            with self.subTest(token=token[:8]), self.assertRaises(siwc.AuthError):
                siwc.validate_id_token(token, client_id="oaiapp_test", nonce=nonce,
                                       signing_key=self.public_key)

    def test_rejects_signed_token_with_wrong_public_key(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(siwc.AuthError):
            siwc.validate_id_token(self._token(), client_id="oaiapp_test", nonce="nonce123",
                                   signing_key=other.public_key())

    def test_full_login_pipeline_with_simulated_loopback_callback(self):
        """Exercise real callback listener and storage, mock only OpenAI endpoints."""
        with TemporaryDirectory() as directory:
            store = siwc.CredentialStore(Path(directory) / "private-data")
            self.assertIsNone(store.load_session())
            captured: dict[str, str] = {}
            callbacks: list[threading.Thread] = []

            def fake_browser(url: str, *, new: int) -> bool:
                query = parse_qs(urlsplit(url).query)
                captured["nonce"] = query["nonce"][0]
                captured["redirect_uri"] = query["redirect_uri"][0]
                captured["host_id"] = query["ext_agent_host_id"][0]

                def callback_request():
                    from urllib.request import urlopen
                    callback_url = (captured["redirect_uri"] + "?state=" + query["state"][0]
                                    + "&code=one-time-code&client_id=oaiapp_test")
                    with urlopen(callback_url, timeout=3) as response:
                        self.assertEqual(response.status, 200)

                thread = threading.Thread(target=callback_request)
                thread.start()
                callbacks.append(thread)
                return True

            def fake_exchange(*, client_id, code, verifier, redirect_uri):
                self.assertEqual(client_id, "oaiapp_test")
                self.assertEqual(code, "one-time-code")
                self.assertEqual(redirect_uri, captured["redirect_uri"])
                self.assertGreater(len(verifier), 40)
                return {"id_token": self._token(nonce=captured["nonce"]),
                        "access_token": "access-secret", "refresh_token": "refresh-secret",
                        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                        "token_type": "Bearer", "expires_in": 3600}

            original_verify = siwc.validate_id_token
            with patch("ai_agent_engine.siwc.webbrowser.open", side_effect=fake_browser), \
                 patch("ai_agent_engine.siwc._exchange_code", side_effect=fake_exchange):
                # Keep the offline test's public key; no JWKS request is performed.
                with patch("ai_agent_engine.siwc.validate_id_token", side_effect=lambda token, **kw:
                           original_verify(token, **{**kw, "signing_key": self.public_key})):
                    result = siwc.login(store, timeout=3)
            for thread in callbacks:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertTrue(result["sharing_enabled"])
            self.assertEqual(store.load_session()["subject"], "user123")
            self.assertEqual(store.host_id(), captured["host_id"])
            self.assertEqual(store.client_id(), "oaiapp_test")
            self.assertIsNone(store._read("pending-registration.json"))


if __name__ == "__main__":
    unittest.main()
