#!/usr/bin/env bash
# Show ClipFlow service status, heartbeat age, posting status and dashboard health.
set -euo pipefail
usage() { echo "usage: $0"; }
case "${1:-}" in -h|--help) usage; exit 0 ;; "") ;; *) usage; exit 1 ;; esac
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
DOMAIN="gui/$(id -u)"
cd "$REPO"
for name in scheduler dashboard; do
  label="com.clipflow.$name"
  if out="$(launchctl print "$DOMAIN/$label" 2>/dev/null)"; then
    state="$(awk -F' = ' '/^\tstate = /{print $2; exit}' <<<"$out")"
    pid="$(awk -F' = ' '/^\tpid = /{print $2; exit}' <<<"$out")"
    last="$(awk -F' = ' '/last exit code = /{print $2; exit}' <<<"$out")"
    echo "$name: loaded, state=${state:-?}, pid=${pid:--}, last exit=${last:--}"
  else
    echo "$name: not loaded"
  fi
done
if [ -f data/heartbeat.txt ]; then
  "$PY" - <<'PYEOF'
from datetime import datetime, timezone
at = datetime.fromisoformat(open("data/heartbeat.txt").read().strip())
print(f"heartbeat: {int((datetime.now(timezone.utc) - at).total_seconds())}s ago")
PYEOF
else
  echo "heartbeat: none"
fi
"$PY" -m app.main posting status 2>/dev/null || echo "posting status: unavailable"
health="$(curl -s --max-time 3 http://127.0.0.1:8000/health || true)"
echo "dashboard health: ${health:-no response}"
