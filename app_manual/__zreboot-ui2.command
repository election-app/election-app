#!/bin/bash
# Start SECONDARY rebooter on health port 9049; watches PRIMARY at 9050
# No logs created anywhere.

set -euo pipefail
cd "$(dirname "$0")" || exit 1

# Env for rebooter2
export UI_PORT=7052
export R2_HEALTH_PORT=7046
export R1_HEALTH_URL="http://127.0.0.1:7047/health"
# Give primary first crack at hub restarts:
export SECONDARY_DEFER_SEC=10
# Optional tuning:
# export CHECK_EVERY=3
# export REQ_TIMEOUT=2
# export BACKOFF_MIN=1
# export BACKOFF_MAX=20

# Launch completely silent (no nohup.out, no log files/folders)
nohup python3 app-reboot-ui2.py >/dev/null 2>&1 &
echo $! > reboot-ui2.pid
disown

echo "Started app-reboot-ui2.py (pid $(cat reboot-ui2.pid)) — health :${R2_HEALTH_PORT}"
