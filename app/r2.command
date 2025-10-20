#!/bin/bash
# Start SECONDARY rebooter on health port 9049; watches PRIMARY at 9050

cd "$(dirname "$0")" || exit 1

# Env for rebooter2
export HUB_PORT=9052
export R2_HEALTH_PORT=9049
export R1_HEALTH_URL="http://127.0.0.1:9050/health"
# Give primary first crack at hub restarts:
export SECONDARY_DEFER_SEC=10
# Optional tuning:
# export CHECK_EVERY=3
# export REQ_TIMEOUT=2
# export BACKOFF_MIN=1
# export BACKOFF_MAX=20

mkdir -p logs
ts="$(date +%Y%m%d-%H%M%S)"
log="logs/reboot2-$ts.log"

nohup python3 app-reboot2.py >> "$log" 2>&1 &
echo $! > reboot2.pid
echo "Started app-reboot2.py (pid $(cat reboot2.pid)) — health :9049 — log $log"
