#!/bin/bash
# __kill_reboot1.command — stop app-reboot1.py background watchdog
# Usage:
#   ./__kill_reboot1.command            # kill watchdog only
#   ./__kill_reboot1.command --also-hub # also kill gunicorn on :9051

cd "$(dirname "$0")" || exit 1

# 1) Stop the watchdog via pidfile
if [[ -f reboot2.pid ]]; then
  pid="$(cat reboot2.pid)"
  if kill -0 "$pid" 2>/dev/null; then
    echo "Stopping app-reboot2.py (pid $pid)…"
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      echo "Process still alive; sending SIGKILL."
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "Watchdog stopped."
  else
    echo "PID in reboot2.pid ($pid) not running; cleaning up stale pidfile."
  fi
  rm -f reboot1.pid
else
  echo "No reboot2.pid file found; best-effort kill by name…"
  pkill -f "app-reboot2.py" 2>/dev/null || true
fi
