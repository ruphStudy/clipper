#!/usr/bin/env bash
# Restart ClipFlow services: scripts/mac/restart.sh [scheduler|dashboard|all]
set -euo pipefail
usage() { echo "usage: $0 [scheduler|dashboard|all]"; }
target="${1:-all}"
case "$target" in
  scheduler|dashboard) names="$target" ;;
  all) names="scheduler dashboard" ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 1 ;;
esac
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
for f in data/logs/launchd-*.log; do  # keep launchd logs under 10 MB
  [ -f "$f" ] || continue
  if [ "$(stat -f%z "$f")" -gt 10485760 ]; then tail -n 2000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"; fi
done
for name in $names; do
  launchctl kickstart -k "gui/$(id -u)/com.clipflow.$name" && echo "restarted $name"
done
