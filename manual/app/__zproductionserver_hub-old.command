#!/bin/bash
cd "$(dirname "$0")"
exec gunicorn -w 1 -b 0.0.0.0:9051 app_hub:app
