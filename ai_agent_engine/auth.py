"""User-facing local SIWC commands: ``python -m ai_agent_engine.auth login``."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import sys
import time

from .session_revoke import sign_out
from .siwc import AuthError, CredentialStore, login


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local ChatGPT sign-in (no API key)")
    parser.add_argument("command", choices=("login", "status", "logout"))
    parser.add_argument("--manual-browser", action="store_true",
                        help="print the sign-in URL for a browser on this same host")
    options = parser.parse_args(argv)
    try:
        store = CredentialStore()
        if options.command == "login":
            info = login(store, open_browser=not options.manual_browser)
            print("Identity verified. Account:", info["email"] or "(no email claim)")
            print("ChatGPT plan permission:", "GRANTED" if info["sharing_enabled"] else "NOT GRANTED")
            if not info["sharing_enabled"]:
                print("Enable ChatGPT plan usage before attempting a model request.")
        elif options.command == "logout":
            revoked = sign_out(store)
            print("Local ChatGPT tokens removed (or no session existed).")
            if revoked:
                print("Remote revocation: CONFIRMED (or there was no saved session).")
            else:
                print("Remote revocation NOT CONFIRMED. Disconnect the app in ChatGPT Settings.")
        else:
            session = store.load_session()
            if session is None:
                print("Not signed in on this computer.")
            else:
                print("Saved ChatGPT identity:", session.get("email") or "(no email claim)")
                print("ChatGPT plan permission:", "GRANTED" if session.get("sharing_enabled") else "NOT GRANTED")
                expiration = session.get("expires_at", 0)
                if (type(expiration) in (int, float)
                        and 0 <= expiration <= 253402300799
                        and math.isfinite(expiration)):
                    try:
                        stamp = datetime.fromtimestamp(expiration, tz=timezone.utc).isoformat()
                    except (OverflowError, OSError, ValueError):
                        print("Saved access-token expiry is invalid; check sign-in safely.")
                    else:
                        print("Access token expires:", stamp)
                        if expiration < time.time():
                            print("Access token expired. The next model request will attempt a safe refresh.")
                else:
                    print("Saved access-token expiry is invalid; check sign-in safely.")
        return 0
    except AuthError as exc:
        print("Login unavailable:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
