#!/usr/bin/env bash
# Append a Nitter session from cookies you copied out of a logged-in x.com tab.
# Use this when the scripted login (gen-session.sh) fails (captcha / verification).
#
#   ./add-session.sh <username>
#
# To avoid disturbing your existing X logins, sign the throwaway account in via a
# Firefox Private Window (Ctrl+Shift+P) -- it has its own cookie jar. Then, in that
# window: F12 -> Storage -> Cookies -> https://x.com, and copy these three values:
#   auth_token, ct0, twid
# Paste each when prompted below (input is hidden). See README.md for details.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <username>" >&2
  exit 1
fi

read -rs -p "auth_token cookie: " TW_AUTH_TOKEN; echo
read -rs -p "ct0 cookie: " TW_CT0; echo
read -rs -p "twid cookie: " TW_TWID; echo
export TW_AUTH_TOKEN TW_CT0 TW_TWID

uv run python add_cookie_session.py "$1" sessions.jsonl

unset TW_AUTH_TOKEN TW_CT0 TW_TWID

echo "sessions.jsonl now has $(wc -l < sessions.jsonl) session line(s)."
echo "Restart Nitter to pick up changes: docker compose -f docker-compose.yml restart nitter"
