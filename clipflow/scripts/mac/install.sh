#!/usr/bin/env bash
# Install ClipFlow as user LaunchAgents (scheduler + dashboard). No sudo.
set -euo pipefail
usage() { echo "usage: $0 [--lan]   (--lan: dashboard on 0.0.0.0:8000 for your phone; needs DASHBOARD_PASSWORD)"; }
LAN=""
case "${1:-}" in
  --lan) LAN="--lan" ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 1 ;;
esac
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
PY="$REPO/.venv/bin/python"
DOMAIN="gui/$(id -u)"
[ -x "$PY" ] || { echo "error: $PY not found — create the venv first (see README)"; exit 1; }
cd "$REPO"
mkdir -p data/logs "$AGENTS"
for f in data/logs/launchd-*.log; do  # keep launchd logs under 10 MB
  [ -f "$f" ] || continue
  if [ "$(stat -f%z "$f")" -gt 10485760 ]; then tail -n 2000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"; fi
done
"$PY" -m app.scheduler.launchd write --out-dir "$AGENTS" ${LAN:+$LAN} >/dev/null
for name in scheduler dashboard; do
  label="com.clipflow.$name"
  plist="$AGENTS/$label.plist"
  plutil -lint "$plist" >/dev/null
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  for attempt in 1 2 3 4 5; do  # bootout is asynchronous; retry until the old job is gone
    if launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then break; fi
    [ "$attempt" = 5 ] && { echo "error: could not load $label"; exit 1; }
    sleep 1
  done
  echo "loaded $label"
done
sleep 3
"$REPO/scripts/mac/status.sh"
