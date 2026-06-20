#!/usr/bin/env bash
# Generate a Nitter session by driving a real Chromium login (nodriver).
# Use this when gen-session.sh (scripted API login) gets blocked by a captcha
# or new-device verification -- a visible browser lets you clear those by hand.
#
#   ./browser-session.sh <username> [--headless]
#
# Needs a Chromium-family browser installed (nodriver does not drive Firefox).
# Prompts (hidden) for the password and 2FA secret and passes them via env vars,
# not argv, so they never show up in ps/proc. Leave the 2FA prompt blank if the
# account has no TOTP 2FA (you can still type an emailed/SMS code in the window).
set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <username> [--headless]" >&2
  exit 1
fi
username="$1"; shift

read -rs -p "Password: " TW_PASSWORD; echo
read -rs -p "2FA secret (blank if none): " TW_2FA_SECRET; echo
export TW_PASSWORD TW_2FA_SECRET

uv run --with-requirements requirements-browser.txt \
  python create_session_browser.py "$username" sessions.jsonl "$@"

unset TW_PASSWORD TW_2FA_SECRET

echo "sessions.jsonl now has $(wc -l < sessions.jsonl) session line(s)."
echo "Restart Nitter to pick up changes: docker compose -f docker-compose.yml restart nitter"
