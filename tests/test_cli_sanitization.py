"""CLI resilience to malformed local state; no live account or network."""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import unittest
from unittest.mock import patch

from ai_agent_engine import auth


class FakeCredentials:
    def __init__(self, expiry):
        self.expiry = expiry

    def load_session(self):
        return {"email": "owner@example.invalid", "sharing_enabled": True,
                "expires_at": self.expiry}


class AuthStatusSafetyTests(unittest.TestCase):
    def test_invalid_timestamp_never_raises_or_prints_stack_trace(self):
        for expiry in (float("inf"), -1, 10 ** 10000, "secret-token", float("nan")):
            with self.subTest(expiry=expiry):
                output, errors = StringIO(), StringIO()
                with patch.object(auth, "CredentialStore", return_value=FakeCredentials(expiry)):
                    with redirect_stdout(output), redirect_stderr(errors):
                        code = auth.main(["status"])
                self.assertEqual(code, 0)
                self.assertIn("expiry is invalid", output.getvalue())
                self.assertNotIn("secret-token", output.getvalue())
                self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
