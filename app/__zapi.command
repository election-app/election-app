#!/bin/bash
# Launch uvicorn API in the background — no logs, save PID, then disown.

set -euo pipefail
cd "$(dirname "$0")" || exit 1

# Config (override via env if desired)
PORT="${API_PORT:-5037}"
APP_MODULE="${API_APP:-api:app}"
WORKERS="${API_WORKERS:-1}"

# Quiet flags: no access log, minimal level
FLAGS="--host 0.0.0.0 --port ${PORT} --workers ${WORKERS} --log-level critical --no-access-log"

# Start silently, record PID, then disown (no nohup.out, no log files)
nohup uvicorn "${APP_MODULE}" ${FLAGS} >/dev/null 2>&1 & echo $! > uvicorn.pid
disown

echo "Started ${APP_MODULE} on :${PORT} (pid $(cat uvicorn.pid)); no logs created."
