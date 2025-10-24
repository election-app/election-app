#!/bin/bash
cd "$(dirname "$0")"

nohup gunicorn -w 1 -b 0.0.0.0:8443 control-panel:app \
  > /tmp/control-panel.out 2>&1 &

disown
