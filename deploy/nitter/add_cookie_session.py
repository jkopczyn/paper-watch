#!/usr/bin/env python3
"""Append a cookie-based Nitter session from values copied out of your browser.

No browser automation and no extra dependencies. You paste the auth_token, ct0 and
twid cookies from a logged-in x.com tab; this writes them to sessions.jsonl in the
format Nitter expects ({"kind": "cookie", ...}).

IMPORTANT (avoid colliding with your existing X logins): log the throwaway account
in via a Firefox Private Window (or a separate profile). A private window has its own
cookie jar, so it does not disturb the account you're signed into in normal windows.

Secrets come from env vars (TW_AUTH_TOKEN / TW_CT0 / TW_TWID) or hidden prompts,
never argv. Only the non-secret username and output path are positional args.

Usage: python3 add_cookie_session.py <username> <path>
"""
import json
import os
import sys
import getpass
from urllib.parse import unquote


def parse_user_id(twid: str) -> str | None:
    """Extract the numeric user id from a twid cookie.

    twid looks like `u=1234567890` or url-encoded `u%3D1234567890`, sometimes
    wrapped in quotes or prefixed with `twid=`.
    """
    if not twid:
        return None
    v = unquote(twid).strip().strip('"')
    if v.startswith("twid="):
        v = v[len("twid="):].strip('"')
    if "u=" in v:
        v = v.split("u=", 1)[1]
    v = v.split("&")[0].strip().strip('"')
    return v or None


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 add_cookie_session.py <username> <path>")
        sys.exit(1)

    username = sys.argv[1].lstrip("@")
    path = sys.argv[2]

    auth_token = os.environ.get("TW_AUTH_TOKEN") or getpass.getpass("auth_token cookie: ")
    ct0 = os.environ.get("TW_CT0") or getpass.getpass("ct0 cookie: ")
    twid = os.environ.get("TW_TWID")
    if twid is None:
        twid = getpass.getpass("twid cookie (for the numeric id): ")

    auth_token = auth_token.strip()
    ct0 = ct0.strip()
    user_id = parse_user_id(twid)

    if not auth_token or not ct0:
        print("auth_token and ct0 are both required.", file=sys.stderr)
        sys.exit(1)
    if not user_id:
        print("Warning: could not parse a user id from twid; writing id=null. "
              "Nitter may reject the session -- re-copy the twid cookie if so.",
              file=sys.stderr)

    entry = {
        "kind": "cookie",
        "username": username,
        "id": user_id,
        "auth_token": auth_token,
        "ct0": ct0,
    }

    try:
        # Create with 0o600 so the token file is never group/world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry) + "\n")
        os.chmod(path, 0o600)
    except Exception as e:
        print(f"Failed to write session: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Appended cookie session for @{username} (id={user_id}) to {path}")
