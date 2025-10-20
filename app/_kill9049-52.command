#!/bin/bash
# kill whatever is bound to port 8050
kill -15 $(lsof -t -i :9049) 2>/dev/null
kill -15 $(lsof -t -i :9050) 2>/dev/null
kill -15 $(lsof -t -i :9052) 2>/dev/null
