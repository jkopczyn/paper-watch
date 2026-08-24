#!/usr/bin/env bash
# Called by paper-watch-alert@.service with the failed unit's name.
# Layered on purpose: append to the log and notify-send first (no Python
# needed), then let `paper-watch alert` do Slack + email best-effort.
set -u
unit="${1:-paper-watch}"; unit="${unit%.service}.service"
cd "$(dirname "$0")/../.." || exit 0

log_file=$(sed -n 's/^\s*log_file:\s*//p' config.yaml | head -1 | tr -d '"'"'"'')
log_file="${log_file:-paper-watch-alerts.log}"

status=$(systemctl --user show -p ExecMainStatus --value "$unit" 2>/dev/null)
# Only this invocation's journal, not the previous ticks' (their errors may
# be unrelated and already fixed).
started=$(systemctl --user show -p ExecMainStartTimestamp --value "$unit" 2>/dev/null)
journal() { journalctl --user -u "$unit" ${started:+--since "$started"} --no-pager -o cat "$@" 2>/dev/null; }
# The last exception line of the failed run, if any; else its last log line.
detail=$(journal | grep -E '^[A-Za-z_.]*(Error|Exception)[:(]' | tail -1)
[ -n "$detail" ] || detail=$(journal | tail -1)
subject="$unit failed (exit ${status:-?})"
body="${detail:-no journal detail}
See: journalctl --user -u $unit -n 80"

printf '%s %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$subject" "$(echo "$body" | tr '\n' ' ')" >> "$log_file"
notify-send --urgency=critical --app-name=paper-watch "$subject" "$body" 2>/dev/null

echo "$body" | "$HOME/.local/bin/uv" run paper-watch alert --subject "$subject" --skip log,desktop
exit 0
