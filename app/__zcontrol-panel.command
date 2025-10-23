#!/bin/bash
cd "$(dirname "$0")"
exec gunicorn -w 1 -b 0.0.0.0:8443 control-panel:app
