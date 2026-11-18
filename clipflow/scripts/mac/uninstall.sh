#!/usr/bin/env bash
# Stop and remove the ClipFlow LaunchAgents.
set -euo pipefail
usage() { echo "usage: $0"; }
case "${1:-}" in -h|--help) usage; exit 0 ;; "") ;; *) usage; exit 1 ;; esac
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
for name in scheduler dashboard; do
  label="com.clipflow.$name"
  if launchctl bootout "$DOMAIN/$label" 2>/dev/null; then echo "stopped $label"; else echo "$label was not loaded"; fi
  rm -f "$AGENTS/$label.plist"
done
echo "removed ClipFlow LaunchAgents"
