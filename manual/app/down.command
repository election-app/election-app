#!/bin/bash
# kill whatever is bound to port 8050
kill -15 $(lsof -t -i :7046) 2>/dev/null
kill -15 $(lsof -t -i :7047) 2>/dev/null
kill -15 $(lsof -t -i :7049) 2>/dev/null
kill -15 $(lsof -t -i :7050) 2>/dev/null
kill -15 $(lsof -t -i :7051) 2>/dev/null
kill -15 $(lsof -t -i :7052) 2>/dev/null
