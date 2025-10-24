#!/bin/bash
cd "$(dirname "$0")"
exec gunicorn -w 10 -b 0.0.0.0:9052 app_ui:app
