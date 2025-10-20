#!/bin/bash
# Start PRIMARY rebooter on health port 9050; watches SECONDARY at 9049

cd "$(dirname "$0")" || exit 1

# Env for rebooter1
export HUB_PORT=9052
export R1_HEALTH_PORT=9050
export R2_HEALTH_URL="http://127.0.0.1:9049/health"
# Optional tuning:
# export CHECK_EVERY=3
# export REQ_TIMEOUT=2
# export BACKOFF_MIN=1
# export BACKOFF_MAX=20

mkdir -p logs
ts="$(date +%Y%m%d-%H%M%S)"
log="logs/reboot1-$ts.log"

nohup python3 app-reboot1.py >> "$log" 2>&1 &
echo $! > reboot1.pid
echo "Started app-reboot1.py (pid $(cat reboot1.pid)) — health :9050 — log $log"
