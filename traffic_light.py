#!/usr/bin/env python3
"""Traffic light on a Raspberry Pi - start here. The code is in trafficlight/, the page in web/.

Run: python3 traffic_light.py, then open http://<pi-ip>:8000
"""
from trafficlight.controller import main

if __name__ == '__main__':
    main()
