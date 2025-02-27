#!/usr/bin/env python3
import os
import sys
import psutil

# Check if the main.py process is running
main_running = False
for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
    if proc.info['cmdline'] and len(proc.info['cmdline']) > 1:
        if 'python' in proc.info['cmdline'][0] and 'main.py' in proc.info['cmdline'][1]:
            main_running = True
            break

if main_running:
    sys.exit(0)
else:
    sys.exit(1)