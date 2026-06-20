#!/usr/bin/env python3
"""Generate a Nitter cookie session by driving a real Chromium login.

Vendored from zedeus/nitter `tools/create_session_browser.py` and hardened to
match the other tools in this directory:

  * Secrets (password, 2FA secret) come from env vars TW_PASSWORD / TW_2FA_SECRET
    or hidden prompts -- NEVER from argv, which leaks via /proc/<pid>/cmdline.
    Only the non-secret username and output path are positional args.
  * The session line is written 0600 so the token file is never world-readable.
  * twid -> user-id parsing is shared with add_cookie_session.parse_user_id.

This uses `nodriver`, which drives a Chromium-family browser (not Firefox), so a
Chromium install is required. By default it runs a visible browser window, which
is far less likely to trip X's bot detection than --headless; solve any captcha
or new-device check in the window and the flow continues automatically.

Usage: python3 create_session_browser.py <username> <path> [--headless]
Set TW_PASSWORD / TW_2FA_SECRET env vars, or you'll be prompted (hidden).
"""

import asyncio
import getpass
import json
import os
import sys

import nodriver as uc
import pyotp

from add_cookie_session import parse_user_id


async def login_and_get_cookies(username, password, totp_seed=None, headless=False):
    """Authenticate with X.com in a real browser and extract session cookies."""
    # headless mode increases bot-detection risk; visible is the default.
    browser = await uc.start(headless=headless)
    tab = await browser.get("https://x.com/i/flow/login")

    try:
        print(f"[*] Entering username {username}...", file=sys.stderr)
        retry = 0
        while retry < 5:
            username_input = await tab.find('input[autocomplete="username"]', timeout=10)
            pos = await username_input.get_position()
            await tab.mouse_move(pos.x, pos.y, steps=50, flash=True)
            await asyncio.sleep(0.1)
            await username_input.click()
            await asyncio.sleep(0.5)
            await username_input.send_keys(username)
            await asyncio.sleep(0.2)
            await username_input.send_keys("\n")
            await asyncio.sleep(2)

            if "Could not log you in" in await tab.get_content():
                retry += 1
                wait = retry * 10
                print(f"[*] Username rejected; retrying in {wait}s...", file=sys.stderr)
                await asyncio.sleep(wait)
            else:
                break

        print("[*] Entering password...", file=sys.stderr)
        pretry = 0
        while pretry < 5:
            password_input = await tab.find(
                'input[autocomplete="current-password"]', timeout=15
            )
            await password_input.click()
            await asyncio.sleep(0.5)
            await password_input.send_keys(password)
            await asyncio.sleep(0.2)
            await password_input.send_keys("\n")
            await asyncio.sleep(2)

            if "Could not log you in" in await tab.get_content():
                pretry += 1
                wait = pretry * 10
                print(f"[*] Password step retrying in {wait}s...", file=sys.stderr)
                await asyncio.sleep(wait)
            else:
                break

        page_content = await tab.get_content()
        if "verification code" in page_content or "Enter code" in page_content:
            if not totp_seed:
                raise Exception(
                    "2FA required but no TOTP secret provided. Set TW_2FA_SECRET "
                    "(the base32 secret, not a 6-digit code), or enter any "
                    "emailed/SMS code in the browser window yourself."
                )
            print("[*] 2FA detected, entering TOTP code...", file=sys.stderr)
            totp_code = pyotp.TOTP(totp_seed).now()
            code_input = await tab.select('input[type="text"]')
            await code_input.send_keys(totp_code + "\n")
            await asyncio.sleep(3)

        print("[*] Waiting for session cookies (solve any challenge in the "
              "window if shown)...", file=sys.stderr)
        for _ in range(60):  # up to ~60s, to allow manual captcha solving
            cookies = await browser.cookies.get_all()
            cookies_dict = {c.name: c.value for c in cookies}
            if "auth_token" in cookies_dict and "ct0" in cookies_dict:
                return {
                    "username": username,
                    "id": parse_user_id(cookies_dict.get("twid", "")),
                    "auth_token": cookies_dict["auth_token"],
                    "ct0": cookies_dict["ct0"],
                }
            await asyncio.sleep(1)

        raise Exception(
            "Timed out waiting for auth_token/ct0 cookies. The login likely "
            "didn't complete (captcha, wrong password, or new-device check)."
        )
    finally:
        browser.stop()


def write_session(entry, path):
    # Create with 0o600 so the token file is never group/world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry) + "\n")
    os.chmod(path, 0o600)  # tighten even if the file pre-existed


async def main():
    args = [a for a in sys.argv[1:]]
    headless = "--headless" in args
    args = [a for a in args if not a.startswith("--")]
    if len(args) != 2:
        print("Usage: python3 create_session_browser.py <username> <path> [--headless]")
        print("Set TW_PASSWORD / TW_2FA_SECRET env vars, or you'll be prompted.")
        sys.exit(1)

    username = args[0].lstrip("@")
    path = args[1]

    password = os.environ.get("TW_PASSWORD") or getpass.getpass("Password: ")
    totp_seed = os.environ.get("TW_2FA_SECRET")
    if totp_seed is None:
        totp_seed = getpass.getpass("2FA secret (blank if none): ")
    totp_seed = totp_seed.strip() or None

    try:
        cookies = await login_and_get_cookies(username, password, totp_seed, headless)
    except Exception as error:
        print(f"[!] Login failed: {error}", file=sys.stderr)
        sys.exit(1)

    if not cookies.get("id"):
        print("[!] Warning: could not parse a user id from the twid cookie; "
              "writing id=null. Nitter may reject the session.", file=sys.stderr)

    entry = {"kind": "cookie", **cookies}
    try:
        write_session(entry, path)
    except Exception as e:
        print(f"[!] Failed to write session: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Appended cookie session for @{username} (id={cookies.get('id')}) to {path}")
    os._exit(0)  # nodriver leaves threads around; exit hard to avoid hangs


if __name__ == "__main__":
    asyncio.run(main())
