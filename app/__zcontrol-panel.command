#!/bin/bash
# Launch control-panel on :9048 in the background — no logs, save PID, then disown.

set -euo pipefail
cd "$(dirname "$0")" || exit 1

PORT=9048
APP_MODULE="control-panel:app"

# (Optional) ensure Gunicorn won’t try to write access/error logs to files
unset GUNICORN_CMD_ARGS
CMD="gunicorn -w 1 -b 0.0.0.0:${PORT} --access-logfile - --error-logfile - --log-level critical --capture-output ${APP_MODULE}"

# Start silently, record PID, then disown
nohup bash -lc "$CMD" >/dev/null 2>&1 & echo $! > control-panel.pid
disown

echo "Started ${APP_MODULE} on :${PORT} (pid $(cat control-panel.pid)); no logs created."
