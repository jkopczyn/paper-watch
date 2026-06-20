#!/usr/bin/env bash
# Generate a Nitter session for ONE X/Twitter account and append it to sessions.jsonl.
# Run this once per throwaway account; Nitter rotates across every line in the file.
#
#   ./gen-session.sh <username> <password> <2fa_secret>
#
# Pass '' for <2fa_secret> if the account has no TOTP 2FA.
# Credentials are passed straight to get_session.py and are NOT stored anywhere.
# Tip: prefix the command with a space so it stays out of your shell history.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 <username> <password> <2fa_secret>   (use '' for 2fa if none)" >&2
  exit 1
fi

uv run --with requests --with pyotp --with cloudscraper \
  python get_session.py "$1" "$2" "$3" sessions.jsonl

echo "sessions.jsonl now has $(wc -l < sessions.jsonl) session line(s)."
echo "Restart Nitter to pick up changes: docker compose -f docker-compose.yml restart nitter"
