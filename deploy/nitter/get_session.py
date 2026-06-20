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

TW_CONSUMER_KEY = '3nVuSoBZnx6U4vzUxf5w'
TW_CONSUMER_SECRET = 'Bcs59EFbbsdF6Sl9Ng71smgStWEGwXXKSjYvPVt7qys'

def auth(username, password, otp_secret):
    bearer_token_req = requests.post("https://api.twitter.com/oauth2/token",
        auth=(TW_CONSUMER_KEY, TW_CONSUMER_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data='grant_type=client_credentials'
    ).json()
    bearer_token = ' '.join(str(x) for x in bearer_token_req.values())

    guest_token = requests.post(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers={
            'Authorization': bearer_token,
             "User-Agent": "TwitterAndroid/10.21.0-release.0 (310210000-r-0) ONEPLUS+A3010/9"
        }
    ).json().get('guest_token')

    if not guest_token:
        print("Failed to obtain guest token.")
        sys.exit(1)

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
        "X-Twitter-Client-DeviceID": ""
    }

    scraper = cloudscraper.create_scraper()
    scraper.headers = twitter_header

    task1 = scraper.post(
        'https://api.twitter.com/1.1/onboarding/task.json',
        params={
            'flow_name': 'login',
            'api_version': '1',
            'known_device_token': '',
            'sim_country_code': 'us'
        },
        json={
            "flow_token": None,
            "input_flow_data": {
                "country_code": None,
                "flow_context": {
                    "referrer_context": {
                        "referral_details": "utm_source=google-play&utm_medium=organic",
                        "referrer_url": ""
                    },
                    "start_location": {
                        "location": "deeplink"
                    }
                },
                "requested_variant": None,
                "target_user_id": 0
            }
        }
    )

    scraper.headers['att'] = task1.headers.get('att')

    task2 = scraper.post(
        'https://api.twitter.com/1.1/onboarding/task.json',
        json={
            "flow_token": task1.json().get('flow_token'),
            "subtask_inputs": [{
                "enter_text": {
                    "suggestion_id": None,
                    "text": username,
                    "link": "next_link"
                },
                "subtask_id": "LoginEnterUserIdentifier"
            }]
        }
    )

    task3 = scraper.post(
        'https://api.twitter.com/1.1/onboarding/task.json',
        json={
            "flow_token": task2.json().get('flow_token'),
            "subtask_inputs": [{
                "enter_password": {
                    "password": password,
                    "link": "next_link"
                },
                "subtask_id": "LoginEnterPassword"
            }],
        }
    )

    for t3_subtask in task3.json().get('subtasks', []):
        if "open_account" in t3_subtask:
            return t3_subtask["open_account"]
        elif "enter_text" in t3_subtask:
            response_text = t3_subtask["enter_text"]["hint_text"]
            totp = pyotp.TOTP(otp_secret)
            generated_code = totp.now()
            task4resp = scraper.post(
                "https://api.twitter.com/1.1/onboarding/task.json",
                json={
                    "flow_token": task3.json().get("flow_token"),
                    "subtask_inputs": [
                        {
                            "enter_text": {
                                "suggestion_id": None,
                                "text": generated_code,
                                "link": "next_link",
                            },
                            "subtask_id": "LoginTwoFactorAuthChallenge",
                        }
                    ],
                }
            )
            task4 = task4resp.json()
            for t4_subtask in task4.get("subtasks", []):
                if "open_account" in t4_subtask:
                    return t4_subtask["open_account"]

    return None

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 get_session.py <username> <path>")
        print("Set TW_PASSWORD / TW_2FA_SECRET env vars, or you'll be prompted.")
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
        print("Authentication failed.")
        sys.exit(1)

    session_entry = {
        "oauth_token": result.get("oauth_token"),
        "oauth_token_secret": result.get("oauth_token_secret")
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
