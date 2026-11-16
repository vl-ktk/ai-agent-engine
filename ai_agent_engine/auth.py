"""User-facing local SIWC commands: ``python -m ai_agent_engine.auth login``."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
import time

from .siwc import AuthError, CredentialStore, login


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local ChatGPT sign-in (no API key)")
    parser.add_argument("command", choices=("login", "status"))
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
        else:
            session = store.load_session()
            if session is None:
                print("Not signed in on this computer.")
            else:
                print("Saved ChatGPT identity:", session.get("email") or "(no email claim)")
                print("ChatGPT plan permission:", "GRANTED" if session.get("sharing_enabled") else "NOT GRANTED")
                expiration = session.get("expires_at", 0)
                if isinstance(expiration, (int, float)):
                    stamp = datetime.fromtimestamp(expiration, tz=timezone.utc).isoformat()
                    print("Access token expires:", stamp)
                    if expiration < time.time():
                        print("Access token expired. Token refresh is a later task; use login again.")
        return 0
    except AuthError as exc:
        print("Login unavailable:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
