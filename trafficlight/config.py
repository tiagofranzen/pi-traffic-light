"""Shared settings: GPIO pins, ports, timings and file locations."""
from __future__ import annotations
import os

# GPIO pins
RED_PIN = 22
YELLOW_PIN = 27
GREEN_PIN = 17
ACTIVE_HIGH = False

# Network
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = 8000

# Timing (seconds)
AUTO_GREEN_DURATION = 20.0
AUTO_YELLOW_DURATION = 3.0
AUTO_RED_DURATION = 20.0
AUTO_RED_YELLOW_DURATION = 2.0
EMERGENCY_BLINK_INTERVAL = 0.5
CONTROLLER_LOOP_SLEEP = 0.2
SSE_KEEPALIVE_SECONDS = 15.0
NETWORK_POLL_INTERVAL = 10.0
LAST_MODE_FILE = os.path.expanduser(os.getenv("LAST_MODE_FILE", "~/.traffic_light_mode.json"))
API_TIMEOUT = 15.0
POLL_RETRY_SECONDS = 30.0      # first retry after a failed fetch; doubles up to the poll interval
POLL_STALE_INTERVALS = 3       # keep the last good data this many poll intervals before showing "no data"
