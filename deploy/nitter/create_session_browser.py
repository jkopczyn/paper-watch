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


# Click a *visible* button/link whose trimmed text matches one of `txts`.
# Done in JS, not nodriver's tab.find(): tab.find() searches ALL text nodes
# including <script> contents, and X's page embeds the word "Next" in its
# __INITIAL_STATE__ script -- so tab.find("Next") clicks a <script> (a no-op),
# which is what made the login hang after the username step. offsetParent !=
# null filters out the hidden/phantom duplicate buttons X keeps in the DOM.
_CLICK_BY_TEXT_JS = """
(txts) => {
  const want = txts.map(t => t.toLowerCase());
  const els = [...document.querySelectorAll('button,[role="button"]')];
  const el = els.find(b =>
    b.offsetParent !== null &&
    want.includes((b.innerText || b.textContent || '').trim().toLowerCase()));
  if (el) { el.click(); return (el.innerText || el.textContent || '').trim(); }
  return null;
}
"""


async def _click_advance(tab, *texts):
    """Click a visible button matching one of `texts`; return its label or None."""
    expr = "(" + _CLICK_BY_TEXT_JS + ")(" + json.dumps(list(texts)) + ")"
    try:
        return await tab.evaluate(expr, return_by_value=True)
    except Exception:
        return None


async def _visible_input(tab, selectors, timeout=3):
    """Return the first *visible*, enabled input matching any selector, else None.

    X renders hidden duplicate inputs (e.g. input[name="password"] exists on the
    username step too), so we must check visibility, not just presence.
    """
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while True:
        for sel in selectors:
            for el in await tab.select_all(sel, timeout=1):
                try:
                    if await el.apply("(e) => e.offsetParent !== null && !e.disabled"):
                        return el
                except Exception:
                    continue
        if loop.time() >= end:
            return None
        await asyncio.sleep(0.5)


async def _cookies_if_logged_in(browser, username):
    cookies = await browser.cookies.get_all()
    cd = {c.name: c.value for c in cookies}
    if "auth_token" in cd and "ct0" in cd:
        return {
            "username": username,
            "id": parse_user_id(cd.get("twid", "")),
            "auth_token": cd["auth_token"],
            "ct0": cd["ct0"],
        }
    return None


async def login_and_get_cookies(username, password, totp_seed=None, headless=False):
    """Drive X's login in a real browser and harvest the session cookies.

    X serves automated browsers a shifting, hostile login flow (the username
    field is `autocomplete~=username` not `name=text`; the button is "Continue"
    or "Log in", not always "Next"; hidden duplicate fields abound). Rather than
    a fixed username->Next->password sequence, this is screen-driven and
    *assisted*: each pass it fills whatever visible field is in front of it and
    clicks the advance button, while polling for the auth cookies -- so if X
    throws a captcha or "verify it's you" step, you can finish it by hand in the
    visible window and the loop still captures the result.
    """
    # headless mode increases bot-detection risk; visible is the default.
    browser = await uc.start(headless=headless)
    tab = await browser.get("https://x.com/i/flow/login")
    await tab.sleep(3)

    username_done = password_done = totp_done = False
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 180  # ~3 min total, incl. manual challenge solving
    nudged = False

    try:
        while loop.time() < deadline:
            got = await _cookies_if_logged_in(browser, username)
            if got:
                return got

            # 1) Username / identifier field.
            if not username_done:
                u = await _visible_input(
                    tab,
                    ['input[autocomplete~="username"]', 'input[name="text"]'],
                    timeout=2,
                )
                if u is not None:
                    print(f"[*] Entering username {username}...", file=sys.stderr)
                    await u.click()
                    await asyncio.sleep(0.3)
                    await u.send_keys(username)
                    await asyncio.sleep(0.6)
                    await _click_advance(tab, "Next", "Continue", "Log in")
                    username_done = True
                    await asyncio.sleep(2.5)
                    continue

            # 2) Password field (only the visible one, not the phantom).
            if not password_done:
                p = await _visible_input(
                    tab,
                    [
                        'input[name="password"]',
                        'input[autocomplete="current-password"]',
                        'input[type="password"]',
                    ],
                    timeout=2,
                )
                if p is not None:
                    print("[*] Entering password...", file=sys.stderr)
                    await p.click()
                    await asyncio.sleep(0.3)
                    await p.send_keys(password)
                    await asyncio.sleep(0.6)
                    await _click_advance(tab, "Log in", "Next", "Continue")
                    password_done = True
                    await asyncio.sleep(2.5)
                    continue

            # 3) TOTP 2FA challenge.
            if not totp_done and totp_seed:
                code_field = await _visible_input(
                    tab,
                    [
                        'input[data-testid="ocfEnterTextTextInput"]',
                        'input[inputmode="numeric"]',
                    ],
                    timeout=2,
                )
                if code_field is not None:
                    print("[*] Entering 2FA code...", file=sys.stderr)
                    try:
                        code = pyotp.TOTP(totp_seed).now()
                    except Exception as exc:
                        raise Exception(
                            f"could not generate a TOTP code: {exc}. Paste the "
                            "base32 SECRET, not a 6-digit code."
                        )
                    await code_field.click()
                    await asyncio.sleep(0.3)
                    await code_field.send_keys(code)
                    await asyncio.sleep(0.6)
                    await _click_advance(tab, "Next", "Continue", "Log in", "Verify")
                    totp_done = True
                    await asyncio.sleep(2.5)
                    continue

            # Nothing we recognise is in front of us: a captcha, a "verify it's
            # you" step, or X's app-download funnel. The window is visible -- the
            # user can finish by hand; we keep polling for cookies.
            if not nudged:
                print("[*] Waiting for login to complete -- if the window shows a "
                      "captcha or verification step, solve it there; cookies will "
                      "be captured automatically.", file=sys.stderr)
                nudged = True
            await asyncio.sleep(2)

        raise Exception(
            "Timed out without reaching a logged-in state. X likely served this "
            "automated browser a hostile flow (captcha / app-download funnel). "
            "Use ./add-session.sh to paste cookies from a normal manual login."
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
