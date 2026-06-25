#!/usr/bin/env python3
"""List public channels, join them, and read recent history."""

import os, sys, requests

TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
if not TOKEN:
    sys.exit("Set SLACK_BOT_TOKEN env var")

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
API = "https://slack.com/api"


def api(method, **kwargs):
    r = requests.post(f"{API}/{method}", headers=HEADERS, json=kwargs)
    data = r.json()
    if not data.get("ok"):
        print(f"  ERROR: {method} → {data.get('error')}", file=sys.stderr)
    return data


def list_channels():
    channels = []
    cursor = None
    while True:
        kwargs = {"types": "public_channel", "limit": 200, "exclude_archived": True}
        if cursor:
            kwargs["cursor"] = cursor
        data = api("conversations.list", **kwargs)
        channels.extend(data.get("channels", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def join_channel(channel_id):
    return api("conversations.join", channel=channel_id)


def read_history(channel_id, limit=5):
    data = api("conversations.history", channel=channel_id, limit=limit)
    return data.get("messages", [])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        for ch in list_channels():
            print(f"{ch['id']}  #{ch['name']}")

    elif cmd == "join":
        for cid in sys.argv[2:]:
            print(f"Joining {cid}...")
            join_channel(cid)

    elif cmd == "history":
        cid = sys.argv[2]
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        for msg in read_history(cid, limit):
            user = msg.get("user", "?")
            text = msg.get("text", "")[:120]
            print(f"  [{user}] {text}")

    else:
        print("Usage:")
        print("  slack_channels.py list")
        print("  slack_channels.py join C0123ABC C0456DEF ...")
        print("  slack_channels.py history C0123ABC [limit]")
