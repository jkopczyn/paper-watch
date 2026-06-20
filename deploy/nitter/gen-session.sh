#!/usr/bin/env bash
# Generate a Nitter session for ONE X/Twitter account and append it to sessions.jsonl.
# Run this once per throwaway account; Nitter rotates across every line in the file.
#
#   ./gen-session.sh <username>
#
# The password and 2FA secret are read interactively (hidden) and passed to
# get_session.py via env vars, NOT argv -- so they never appear in `ps` / /proc.
# Leave the 2FA prompt blank if the account has no TOTP 2FA.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <username>" >&2
  exit 1
fi
username="$1"

read -rs -p "Password for @$username: " TW_PASSWORD; echo
read -rs -p "2FA secret (blank if none): " TW_2FA_SECRET; echo
export TW_PASSWORD TW_2FA_SECRET

# Pinned, hash-locked deps (these handle credentials, so don't float them).
TW_PASSWORD="$TW_PASSWORD" TW_2FA_SECRET="$TW_2FA_SECRET" \
  uv run --with-requirements requirements-session.txt \
  python get_session.py "$username" sessions.jsonl

unset TW_PASSWORD TW_2FA_SECRET

echo "sessions.jsonl now has $(wc -l < sessions.jsonl) session line(s)."
echo "Restart Nitter to pick up changes: docker compose -f docker-compose.yml restart nitter"
