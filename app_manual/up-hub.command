#!/bin/bash
# Launch app_hub with gunicorn in the background, NO LOG FILES, write PID, then disown.

set -euo pipefail
cd "$(dirname "$0")" || exit 1

# Config (override via env if desired)
PORT="${HUB_PORT:-7051}"
APP_MODULE="${HUB_APP:-app_hub:app}"

# Make sure Gunicorn won't write access/error logs to files via env defaults
unset GUNICORN_CMD_ARGS

# Build a quiet command (logs to stdout/stderr only), which we then drop to /dev/null
CMD="gunicorn -w 1 -b 0.0.0.0:${PORT} \
  --access-logfile - --error-logfile - --log-level critical \
  --capture-output ${APP_MODULE}"

# Start in background, discard all output (no nohup.out, no log files), record PID, then disown
nohup bash -lc "$CMD" >/dev/null 2>&1 & echo $! > gunicorn.pid
disown

echo "Started ${APP_MODULE} on :${PORT} (pid $(cat gunicorn.pid)); no logs created."
