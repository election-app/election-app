#!/bin/bash
# Start PRIMARY UI rebooter on health port 9047; watches SECONDARY at 9046
# No logs created anywhere.

set -euo pipefail
cd "$(dirname "$0")" || exit 1

# Env for rebooter1
export UI_PORT=7052
export R1_HEALTH_PORT=7047
export R2_HEALTH_URL="http://127.0.0.1:7046/health"
# Optional tuning:
# export CHECK_EVERY=3
# export REQ_TIMEOUT=2
# export BACKOFF_MIN=1
# export BACKOFF_MAX=20

# Ensure Gunicorn (spawned by the rebooter) won't inherit any file-logging args
unset GUNICORN_CMD_ARGS

# Launch completely silent (no nohup.out, no log files/folders)
nohup python3 app-reboot-ui1.py >/dev/null 2>&1 &
echo $! > reboot-ui1.pid
disown

echo "Started app-reboot-ui1.py (pid $(cat reboot-ui1.pid)) — health :${R1_HEALTH_PORT}"
