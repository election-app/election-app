#!/bin/bash
# Start PRIMARY rebooter on health port 9050; watches SECONDARY at 9049
# No logs created anywhere.

set -euo pipefail
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

# Launch completely silent (no nohup.out, no files)
nohup python3 app-reboot1.py >/dev/null 2>&1 &
echo $! > reboot1.pid
disown

echo "Started app-reboot1.py (pid $(cat reboot1.pid)) — health :${R1_HEALTH_PORT}"
