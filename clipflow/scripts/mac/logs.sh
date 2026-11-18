#!/usr/bin/env bash
# Tail ClipFlow logs: scripts/mac/logs.sh [scheduler|dashboard|app] [-f]
set -euo pipefail
usage() { echo "usage: $0 [scheduler|dashboard|app] [-f]"; }
which="${1:-app}"
follow="${2:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
case "$which" in
  scheduler|dashboard) file="$REPO/data/logs/launchd-$which.log" ;;
  app) file="$REPO/data/logs/clipflow.log" ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 1 ;;
esac
[ -f "$file" ] || { echo "no log yet: $file"; exit 0; }
case "$follow" in
  -f) exec tail -n 100 -f "$file" ;;
  "") exec tail -n 100 "$file" ;;
  *) usage; exit 1 ;;
esac
