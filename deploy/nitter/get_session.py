#!/usr/bin/env python3
import requests
import json
import os
import sys
import getpass
import pyotp
import cloudscraper

# NOTE: pyotp, requests and cloudscraper are dependencies; install pinned via
# `uv run --with-requirements requirements-session.txt` (see gen-session.sh).
#
# Secrets are NOT taken from argv (which leaks via /proc/<pid>/cmdline). The
# password and 2FA secret come from env vars TW_PASSWORD / TW_2FA_SECRET, or are
# prompted for interactively (hidden). Only the non-secret username and output
# path are positional args.
#
# Set TW_DEBUG=1 to dump every HTTP status + body to stderr.

TW_CONSUMER_KEY = '3nVuSoBZnx6U4vzUxf5w'
TW_CONSUMER_SECRET = 'Bcs59EFbbsdF6Sl9Ng71smgStWEGwXXKSjYvPVt7qys'

DEBUG = bool(os.environ.get("TW_DEBUG"))

# Known login subtasks X returns when the simple user/pass flow can't complete.
SUBTASK_HELP = {
    "LoginAcid":
        "X wants an extra verification code (email or phone) for this new-device "
        "login. This automated flow can't enter it -- use the browser-based "
        "generator (create_session_browser.py) instead.",
    "LoginTwoFactorAuthChallenge":
        "The account has 2FA enabled but no/!invalid TOTP secret was provided. "
        "Pass the account's base32 2FA SECRET (not the 6-digit code) at the "
        "'2FA secret' prompt.",
    "DenyLoginSubtask":
        "X denied the login (flagged this request as automated). Try the "
        "browser-based generator, or a different account/IP.",
    "ArkoseLogin":
        "X is demanding a FunCaptcha/Arkose challenge. The scripted flow can't "
        "solve it -- use the browser-based generator (create_session_browser.py).",
    "LoginEnterAlternateIdentifierSubtask":
        "X is asking for an alternate identifier (phone/email) because the "
        "username alone wasn't accepted. Try logging in with the full email or "
        "phone as <username>.",
}


def _err(msg):
    print(f"  ! {msg}", file=sys.stderr)


def _debug(label, resp):
    if DEBUG:
        print(f"[debug] {label}: HTTP {resp.status_code}\n{resp.text}\n",
              file=sys.stderr)


def _parse(resp, label):
    """Return parsed JSON, or None after printing a diagnostic."""
    _debug(label, resp)
    try:
        data = resp.json()
    except Exception:
        _err(f"{label}: HTTP {resp.status_code}, non-JSON response:")
        _err(resp.text[:500] or "(empty body)")
        return None
    errors = data.get("errors") if isinstance(data, dict) else None
    if errors:
        for e in errors:
            _err(f"{label}: X error {e.get('code')}: {e.get('message')}")
        return None
    if resp.status_code >= 400:
        _err(f"{label}: HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    return data


def _flow_token(data, label):
    token = data.get("flow_token") if isinstance(data, dict) else None
    if not token:
        _err(f"{label}: no flow_token in response (login flow broke here).")
        if isinstance(data, dict):
            ids = [s.get("subtask_id") for s in data.get("subtasks", [])]
            if ids:
                _err(f"{label}: subtasks returned: {ids}")
    return token


def _report_unhandled(subtasks):
    ids = [s.get("subtask_id") for s in subtasks]
    _err(f"login returned no account credentials. Subtasks: {ids or '(none)'}")
    for sid in ids:
        if sid in SUBTASK_HELP:
            _err(f"-> {sid}: {SUBTASK_HELP[sid]}")


def auth(username, password, otp_secret):
    bearer_resp = requests.post(
        "https://api.twitter.com/oauth2/token",
        auth=(TW_CONSUMER_KEY, TW_CONSUMER_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data='grant_type=client_credentials',
    )
    bearer_data = _parse(bearer_resp, "bearer token")
    if not bearer_data or "access_token" not in bearer_data:
        _err("could not obtain a bearer token (the legacy app key may be blocked).")
        return None
    bearer_token = f"{bearer_data['token_type']} {bearer_data['access_token']}"

    guest_resp = requests.post(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers={
            'Authorization': bearer_token,
            "User-Agent": "TwitterAndroid/10.21.0-release.0 (310210000-r-0) ONEPLUS+A3010/9",
        },
    )
    guest_data = _parse(guest_resp, "guest token")
    guest_token = guest_data.get("guest_token") if guest_data else None
    if not guest_token:
        _err("could not obtain a guest token; cannot start the login flow.")
        return None

    twitter_header = {
        'Authorization': bearer_token,
        "Content-Type": "application/json",
        "User-Agent": "TwitterAndroid/10.21.0-release.0 (310210000-r-0) ONEPLUS+A3010/9 (OnePlus;ONEPLUS+A3010;OnePlus;OnePlus3;0;;1;2016)",
        "X-Twitter-API-Version": '5',
        "X-Twitter-Client": "TwitterAndroid",
        "X-Twitter-Client-Version": "10.21.0-release.0",
        "OS-Version": "28",
        "System-User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ONEPLUS A3010 Build/PKQ1.181203.001)",
        "X-Twitter-Active-User": "yes",
        "X-Guest-Token": guest_token,
        "X-Twitter-Client-DeviceID": "",
    }

    scraper = cloudscraper.create_scraper()
    scraper.headers = twitter_header

    task1 = scraper.post(
        'https://api.twitter.com/1.1/onboarding/task.json',
        params={
            'flow_name': 'login',
            'api_version': '1',
            'known_device_token': '',
            'sim_country_code': 'us',
        },
        json={
            "flow_token": None,
            "input_flow_data": {
                "country_code": None,
                "flow_context": {
                    "referrer_context": {
                        "referral_details": "utm_source=google-play&utm_medium=organic",
                        "referrer_url": "",
                    },
                    "start_location": {"location": "deeplink"},
                },
                "requested_variant": None,
                "target_user_id": 0,
            },
        },
    )
    data1 = _parse(task1, "flow init")
    if not data1 or not _flow_token(data1, "flow init"):
        return None

    scraper.headers['att'] = task1.headers.get('att')

    task2 = scraper.post(
        'https://api.twitter.com/1.1/onboarding/task.json',
        json={
            "flow_token": data1.get('flow_token'),
            "subtask_inputs": [{
                "enter_text": {
                    "suggestion_id": None,
                    "text": username,
                    "link": "next_link",
                },
                "subtask_id": "LoginEnterUserIdentifier",
            }],
        },
    )
    data2 = _parse(task2, "username step")
    if not data2 or not _flow_token(data2, "username step"):
        _err("the username/identifier was rejected. Double-check it, or try the "
             "account's email or phone instead.")
        return None

    task3 = scraper.post(
        'https://api.twitter.com/1.1/onboarding/task.json',
        json={
            "flow_token": data2.get('flow_token'),
            "subtask_inputs": [{
                "enter_password": {
                    "password": password,
                    "link": "next_link",
                },
                "subtask_id": "LoginEnterPassword",
            }],
        },
    )
    data3 = _parse(task3, "password step")
    if not data3:
        _err("the password step failed (often a wrong password or a flagged login).")
        return None

    subtasks = data3.get('subtasks', [])
    for t3_subtask in subtasks:
        if "open_account" in t3_subtask:
            return t3_subtask["open_account"]
        elif t3_subtask.get("subtask_id") == "LoginTwoFactorAuthChallenge":
            if not otp_secret:
                _err(SUBTASK_HELP["LoginTwoFactorAuthChallenge"])
                return None
            try:
                generated_code = pyotp.TOTP(otp_secret).now()
            except Exception as exc:
                _err(f"could not generate a TOTP code from the 2FA secret: {exc}")
                _err("Make sure you pasted the base32 SECRET, not a 6-digit code.")
                return None
            task4 = scraper.post(
                "https://api.twitter.com/1.1/onboarding/task.json",
                json={
                    "flow_token": data3.get("flow_token"),
                    "subtask_inputs": [{
                        "enter_text": {
                            "suggestion_id": None,
                            "text": generated_code,
                            "link": "next_link",
                        },
                        "subtask_id": "LoginTwoFactorAuthChallenge",
                    }],
                },
            )
            data4 = _parse(task4, "2FA step")
            if not data4:
                _err("the 2FA code was rejected (clock skew or wrong secret?).")
                return None
            for t4_subtask in data4.get("subtasks", []):
                if "open_account" in t4_subtask:
                    return t4_subtask["open_account"]
            _report_unhandled(data4.get("subtasks", []))
            return None

    _report_unhandled(subtasks)
    return None


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 get_session.py <username> <path>")
        print("Set TW_PASSWORD / TW_2FA_SECRET env vars, or you'll be prompted.")
        print("Set TW_DEBUG=1 for full HTTP traces.")
        sys.exit(1)

    username = sys.argv[1]
    path = sys.argv[2]

    # Read secrets from env (set by gen-session.sh) or prompt; never from argv.
    password = os.environ.get("TW_PASSWORD") or getpass.getpass("Password: ")
    otp_secret = os.environ.get("TW_2FA_SECRET")
    if otp_secret is None:
        otp_secret = getpass.getpass("2FA secret (blank if none): ")

    result = auth(username, password, otp_secret)
    if result is None:
        print("\nAuthentication failed -- see the diagnostics above.", file=sys.stderr)
        print("If the cause is a captcha/extra-verification/denied-login subtask, "
              "this scripted flow can't proceed; use upstream's browser-based "
              "generator: tools/create_session_browser.py (needs `nodriver`).",
              file=sys.stderr)
        print("Re-run with TW_DEBUG=1 for full HTTP traces.", file=sys.stderr)
        sys.exit(1)

    if not result.get("oauth_token") or not result.get("oauth_token_secret"):
        print(f"Login succeeded but no oauth tokens were returned: {result}",
              file=sys.stderr)
        sys.exit(1)

    session_entry = {
        "oauth_token": result.get("oauth_token"),
        "oauth_token_secret": result.get("oauth_token_secret"),
    }

    try:
        # Create with 0o600 so the token file is never group/world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(session_entry) + "\n")
        os.chmod(path, 0o600)  # tighten even if the file pre-existed
        print("Authentication successful. Session appended to", path)
    except Exception as e:
        print(f"Failed to write session information: {e}")
        sys.exit(1)
