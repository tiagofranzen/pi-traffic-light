#!/usr/bin/env python3
"""Single-file Traffic Light Control System (fully consolidated).

Includes: configuration, shared state, hardware abstraction (real/mock),
mode handlers, monitors (S-Bahn, weather, space, traffic, iRacing UDP),
web server, controller class, and main entry point.

Run: python3 traffic_light/traffic_light_single.py
Web UI: http://<pi-ip>:8000
"""
from __future__ import annotations
import os
import sys
import signal
import logging
import threading
from time import time, sleep
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, List, Tuple
from datetime import datetime, timedelta
import random
import socket
import fcntl
import struct
import ipaddress
import json
import xml.etree.ElementTree as ET
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# --------------------------- Configuration ---------------------------------
# GPIO pins
RED_PIN = 22
YELLOW_PIN = 27
GREEN_PIN = 17
ACTIVE_HIGH = False

# Network
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = 8000
IRACING_UDP_HOST = "0.0.0.0"
IRACING_UDP_PORT = 9001

# Timing (seconds)
AUTO_GREEN_DURATION = 20.0
AUTO_YELLOW_DURATION = 3.0
AUTO_RED_DURATION = 20.0
AUTO_RED_YELLOW_DURATION = 2.0
EMERGENCY_BLINK_INTERVAL = 0.5
PARTY_BLINK_INTERVAL = 0.08
CONTROLLER_LOOP_SLEEP = 0.2
RACING_STEP_DURATION = 1.0
S_BAHN_POLL_INTERVAL = 30.0
WEATHER_POLL_INTERVAL = 900.0
SPACE_POLL_INTERVAL = 900.0
TRAFFIC_POLL_INTERVAL = 600.0
NETWORK_POLL_INTERVAL = 10.0
TRANSIT_POLL_INTERVAL = 600.0
ENVIRONMENT_POLL_INTERVAL = 1800.0
GREMIO_POLL_INTERVAL = 3600.0
API_TIMEOUT = 15.0
AUDIO_INPUT_DEVICE = os.getenv("AUDIO_INPUT_DEVICE", "").strip()

# Audio VU tuning
AUDIO_VU_NOISE_FLOOR = 0.09
AUDIO_VU_ATTACK_SECONDS = 0.02
AUDIO_VU_RELEASE_SECONDS = 0.06
AUDIO_VU_STALE_SECONDS = 0.12
AUDIO_VU_MODE_LOOP_SECONDS = 0.02
AUDIO_VU_SMOOTHING_ATTACK = 0.05
AUDIO_VU_SMOOTHING_RELEASE = 0.30
AUDIO_VU_REFERENCE_LEVEL = 0.28
AUDIO_VU_RELAY_IGNORE_SECONDS = 0.12
AUDIO_VU_MIN_STEP_SECONDS = 0.09
AUDIO_VU_LOUD_GATE = 0.30
AUDIO_VU_BEAT_DELTA = 0.14
AUDIO_VU_BEAT_HOLD_SECONDS = 0.10
AUDIO_VU_BEAT_MIN_INTERVAL = 0.12

# Location / routes
WEATHER_LAT = "48.0667"
WEATHER_LON = "11.7167"
S_BAHN_EVA = "8004733"  # Ottobrunn
OUTBOUND_DESTINATIONS: Tuple[str, ...] = (
    "Kreuzstraße","Aying","Höhenkirchen-Siegertsbrunn","Dürrnhaar","Hohenbrunn","Wächterhof"
)
# Public-transport commute uses the same home -> workplace addresses as the
# 'commute' entry in TRAFFIC_ROUTES (see below). Addresses are geocoded via
# TomTom and passed to MVG (Munich transit authority, free, no key).
MVG_ROUTES_URL = "https://www.mvg.de/api/bgw-pt/v3/routes"
TRAFFIC_ROUTES: Tuple[dict, ...] = (
    {"name": "commute","origin": "Nelkenstraße 24A, 85521 Hohenbrunn, Germany","destination": "Landaubogen 1, 81373 München, Germany"},
    {"name": "center","origin": "Hohenbrunn, Germany","destination": "Marienplatz, Munich, Germany"},
    {"name": "north","origin": "Hohenbrunn, Germany","destination": "BMW Welt, Munich, Germany"},
)

# API keys (env)
DB_CLIENT_ID = os.getenv("DB_CLIENT_ID", "")
DB_CLIENT_SECRET = os.getenv("DB_CLIENT_SECRET", "")
OWM_API_KEY = os.getenv("OWM_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "")

# API URLs
DB_API_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/plan"
WEATHER_API_URL_TEMPLATE = "https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric"
SPACE_WEATHER_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
GOOGLE_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
TOMTOM_GEOCODE_URL = "https://api.tomtom.com/search/2/geocode/{query}.json"
TOMTOM_ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute/{loc}/json"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
GREMIO_ESPN_TEAM_ID = "6273"
GREMIO_ESPN_LEAGUE = "bra.1"
GREMIO_ESPN_SCHEDULE_URL = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{GREMIO_ESPN_LEAGUE}/teams/{GREMIO_ESPN_TEAM_ID}/schedule"
GREMIO_ESPN_SCOREBOARD_URL = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{GREMIO_ESPN_LEAGUE}/scoreboard"

# In-process geocode cache for TomTom (address -> (lat, lon))
_TOMTOM_GEOCODE_CACHE: Dict[str, Tuple[float, float]] = {}

# --------------------------- Shared State ----------------------------------
@dataclass
class SharedState:
    target_mode: str = "auto"
    target_manual_color: str = "off"
    current_mode: str = "auto"
    current_color: str = "unknown"
    last_state_change_time: float = field(default_factory=time)
    s_bahn_minutes_away: int = -1
    s_bahn_transit_status: Dict = field(default_factory=dict)
    weather_status: Dict = field(default_factory=dict)
    iracing_light_status: str = "black"
    iracing_rev_pct: Optional[float] = None
    space_weather_status: Dict = field(default_factory=dict)
    traffic_status: Dict = field(default_factory=dict)
    environment_status: Dict = field(default_factory=dict)
    gremio_status: Dict = field(default_factory=dict)
    audio_vu_level: float = 0.0
    network_connected: bool = False
    mode_state: Dict = field(default_factory=lambda: {
        'next_auto_state': 'green',
        'sos_index': 0,
        'race_step': 0,
        'sos_pattern': [
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.4},
            {'state': 'all_on', 'duration': 0.6}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.6}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.6}, {'state': 'off', 'duration': 0.4},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 1.5},
        ],
        'audio_vu_last': 0.0,
        'audio_vu_above_since': None,
        'audio_vu_below_since': None,
        'audio_vu_ignore_until': 0.0,
        'audio_vu_last_step_time': 0.0,
        'audio_vu_baseline': 0.0,
        'audio_vu_last_beat': 0.0,
        'audio_vu_beat_hold_until': 0.0,
    })
    lock: threading.RLock = field(default_factory=threading.RLock)
    running: bool = True

    def snapshot(self) -> Dict:
        return {
            'color': self.current_color,
            'mode': self.current_mode,
            's_bahn_minutes': self.s_bahn_minutes_away,
            's_bahn_transit': self.s_bahn_transit_status.copy(),
            'weather': self.weather_status.copy(),
            'race_step': self.mode_state.get('race_step', 0),
            'iracing_rev_pct': self.iracing_rev_pct,
            'space_weather': self.space_weather_status.copy(),
            'traffic': self.traffic_status.copy(),
            'environment': self.environment_status.copy(),
            'gremio': self.gremio_status.copy(),
            'audio_level': self.audio_vu_level,
            'network_connected': self.network_connected,
        }

# --------------------------- Hardware --------------------------------------
class LEDInterface(Protocol):
    def on(self) -> None: ...
    def off(self) -> None: ...
    def close(self) -> None: ...

class HardwareLED:
    def __init__(self, pin: int, active_high: bool = False):
        from gpiozero import LED  # Lazy import
        self._led = LED(pin, active_high=active_high)
    def on(self) -> None: self._led.on()
    def off(self) -> None: self._led.off()
    def close(self) -> None: self._led.close()

class MockLED:
    def __init__(self, pin: int, active_high: bool = False):
        self.pin = pin; self.active_high = active_high; self.is_on = False
    def on(self) -> None: self.is_on = True
    def off(self) -> None: self.is_on = False
    def close(self) -> None: pass

class LightHardware:
    def __init__(self, use_mock: bool = False):
        led_cls = MockLED if use_mock else HardwareLED
        created: List[LEDInterface] = []
        try:
            self.red = led_cls(RED_PIN, ACTIVE_HIGH); created.append(self.red)
            self.yellow = led_cls(YELLOW_PIN, ACTIVE_HIGH); created.append(self.yellow)
            self.green = led_cls(GREEN_PIN, ACTIVE_HIGH); created.append(self.green)
        except Exception:
            # Release any pins this attempt already claimed so a retry isn't
            # blocked by our own leaked handles on top of the original error.
            for led in created:
                try: led.close()
                except Exception: pass
            raise
        self.all_lights: List[LEDInterface] = [self.red, self.yellow, self.green]
    def set_state(self, color: str) -> None:
        for l in self.all_lights: l.off()
        if color == "red": self.red.on()
        elif color == "yellow": self.yellow.on()
        elif color == "green": self.green.on()
        elif color == "red_and_yellow": self.red.on(); self.yellow.on()
        elif color == "all_on": self.red.on(); self.yellow.on(); self.green.on()
        elif color == "green-yellow": self.green.on(); self.yellow.on()
    def all_off(self) -> None:
        for l in self.all_lights: l.off()
    def test_sequence(self, duration: float = 0.15) -> None:
        for l in self.all_lights: l.on(); sleep(duration); l.off()
    def cleanup(self) -> None:
        try:
            import RPi.GPIO as GPIO
            self.all_off(); GPIO.cleanup()
        except Exception:
            pass

# --------------------------- Mode Handlers ---------------------------------
# Each returns optional custom sleep value.

def handle_auto_mode(controller, elapsed: float) -> Optional[float]:
    s = controller.state; c = s.current_color
    if c == 'green' and elapsed > AUTO_GREEN_DURATION:
        controller.set_light_state('yellow'); s.mode_state['next_auto_state'] = 'red'; s.last_state_change_time = time()
    elif c == 'yellow' and elapsed > AUTO_YELLOW_DURATION:
        controller.set_light_state(s.mode_state['next_auto_state']); s.last_state_change_time = time()
    elif c == 'red' and elapsed > AUTO_RED_DURATION:
        controller.set_light_state('red_and_yellow'); s.mode_state['next_auto_state'] = 'green'; s.last_state_change_time = time()
    elif c == 'red_and_yellow' and elapsed > AUTO_RED_YELLOW_DURATION:
        controller.set_light_state(s.mode_state['next_auto_state']); s.last_state_change_time = time()

def handle_party_mode(controller, elapsed: float) -> Optional[float]:
    controller.set_light_state(random.choice(['red','yellow','green','off'])); return PARTY_BLINK_INTERVAL

def handle_emergency_mode(controller, elapsed: float) -> Optional[float]:
    controller.set_light_state('yellow' if controller.state.current_color != 'yellow' else 'off'); return EMERGENCY_BLINK_INTERVAL

def handle_sos_mode(controller, elapsed: float) -> Optional[float]:
    s = controller.state; pattern = s.mode_state['sos_pattern']; idx = s.mode_state['sos_index']; step = pattern[idx]
    if elapsed > step['duration']:
        s.mode_state['sos_index'] = (idx + 1) % len(pattern)
        controller.set_light_state(pattern[s.mode_state['sos_index']]['state']); s.last_state_change_time = time()

def handle_s_bahn_mode(controller, elapsed: float) -> Optional[float]:
    mins = controller.state.s_bahn_minutes_away; c = controller.state.current_color
    if mins == -1: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif mins < 9: controller.set_light_state('red')
    elif mins == 9: controller.set_light_state('yellow' if c != 'yellow' else 'off'); return 0.5
    elif mins <= 12: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_biergarten_mode(controller, elapsed: float) -> Optional[float]:
    w = controller.state.weather_status; temp = w.get('temp'); cond = w.get('condition'); hour = datetime.now().hour; c = controller.state.current_color
    if temp is None or cond is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif hour < 16 or temp < 15 or 'Rain' in cond or 'Snow' in cond: controller.set_light_state('red')
    elif temp < 18 or 'Clouds' in cond: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_racing_mode(controller, elapsed: float) -> Optional[float]:
    s = controller.state; step = s.mode_state['race_step']
    if step < 4:
        if step == 0 and elapsed > RACING_STEP_DURATION: controller.set_light_state('red'); s.mode_state['race_step'] += 1; s.last_state_change_time = time()
        elif step == 1 and elapsed > RACING_STEP_DURATION: controller.set_light_state('red_and_yellow'); s.mode_state['race_step'] += 1; s.last_state_change_time = time()
        elif step == 2 and elapsed > RACING_STEP_DURATION: controller.set_light_state('all_on'); s.mode_state['race_step'] += 1; s.last_state_change_time = time()
        elif step == 3 and elapsed > RACING_STEP_DURATION: controller.set_light_state('off'); s.mode_state['race_step'] += 1; s.last_state_change_time = time()
    else:
        live = s.iracing_light_status if s.iracing_light_status != 'black' else 'off'; controller.set_light_state(live); return 0.05

def handle_space_mode(controller, elapsed: float) -> Optional[float]:
    kp = controller.state.space_weather_status.get('kp_index'); c = controller.state.current_color
    if kp is None or kp >= 5: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif kp == 4: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_stau_mode(controller, elapsed: float) -> Optional[float]:
    delay = controller.state.traffic_status.get('avg_delay'); c = controller.state.current_color
    if delay is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif delay > 45: controller.set_light_state('red')
    elif delay > 20: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_uv_mode(controller, elapsed: float) -> Optional[float]:
    env = controller.state.environment_status
    uv = env.get('uv_index')
    c = controller.state.current_color
    if uv is None:
        # No data yet: slow red blink to signal "waiting".
        controller.set_light_state('red' if c != 'red' else 'off')
        return 0.5
    if uv >= 8:
        # Very high / extreme: fast red blink as a warning.
        controller.set_light_state('red' if c != 'red' else 'off')
        return 0.4
    if uv >= 6:
        controller.set_light_state('red')
    elif uv >= 3:
        controller.set_light_state('yellow')
    else:
        controller.set_light_state('green')
    return None

def handle_gremio_mode(controller, elapsed: float) -> Optional[float]:
    result = (controller.state.gremio_status or {}).get('result')
    c = controller.state.current_color
    if result == 'win':
        controller.set_light_state('green')
    elif result == 'draw':
        controller.set_light_state('yellow')
    elif result == 'loss':
        controller.set_light_state('red')
    else:
        # No data: slow red blink.
        controller.set_light_state('red' if c != 'red' else 'off')
        return 0.5
    return None

def handle_audio_vu_mode(controller, elapsed: float) -> Optional[float]:
    state = controller.state
    now = time()

    ignore_until = float(state.mode_state.get('audio_vu_ignore_until', 0.0) or 0.0)
    if now < ignore_until:
        return AUDIO_VU_MODE_LOOP_SECONDS

    last = state.mode_state.get('audio_vu_last', 0.0)
    level = state.audio_vu_level if (now - last) <= AUDIO_VU_STALE_SECONDS else 0.0

    above_since = state.mode_state.get('audio_vu_above_since')
    below_since = state.mode_state.get('audio_vu_below_since')
    above_floor = level >= AUDIO_VU_NOISE_FLOOR

    if above_floor:
        if above_since is None:
            state.mode_state['audio_vu_above_since'] = now
        state.mode_state['audio_vu_below_since'] = None
    else:
        if below_since is None:
            state.mode_state['audio_vu_below_since'] = now
        state.mode_state['audio_vu_above_since'] = None

    # Debounce relay click noise: require sustained signal before turning on,
    # and sustained silence before turning off.
    if state.current_color == 'off':
        above_since = state.mode_state.get('audio_vu_above_since')
        if above_since is None or (now - above_since) < AUDIO_VU_ATTACK_SECONDS:
            return AUDIO_VU_MODE_LOOP_SECONDS
    else:
        below_since = state.mode_state.get('audio_vu_below_since')
        if below_since is not None and (now - below_since) >= AUDIO_VU_RELEASE_SECONDS:
            controller.set_light_state('off')
            return AUDIO_VU_MODE_LOOP_SECONDS

    if level < AUDIO_VU_NOISE_FLOOR:
        target_color = 'off'
    elif level < 0.45:
        target_color = 'green'
    elif level < 0.80:
        target_color = 'green-yellow'
    else:
        target_color = 'all_on'

    if target_color != state.current_color:
        last_step = float(state.mode_state.get('audio_vu_last_step_time', 0.0) or 0.0)
        if (now - last_step) < AUDIO_VU_MIN_STEP_SECONDS:
            return AUDIO_VU_MODE_LOOP_SECONDS

    controller.set_light_state(target_color)
    return AUDIO_VU_MODE_LOOP_SECONDS

# --------------------------- Monitors --------------------------------------
def _get_interface_ipv4(ifname: str) -> Optional[str]:
    """Return IPv4 address for an interface (Linux), or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifreq = struct.pack('256s', ifname.encode('utf-8')[:15])
        # SIOCGIFADDR = 0x8915
        res = fcntl.ioctl(sock.fileno(), 0x8915, ifreq)
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _is_network_connected() -> bool:
    """True if any non-loopback interface is up and has a non-link-local IPv4."""
    try:
        for ifname in os.listdir('/sys/class/net'):
            if ifname == 'lo':
                continue
            try:
                with open(f'/sys/class/net/{ifname}/operstate', 'r', encoding='utf-8') as f:
                    operstate = f.read().strip()
                if operstate != 'up':
                    continue
            except Exception:
                continue

            ip = _get_interface_ipv4(ifname)
            if not ip:
                continue
            try:
                addr = ipaddress.ip_address(ip)
                if addr.version == 4 and not addr.is_link_local:
                    return True
            except ValueError:
                continue
    except Exception:
        return False
    return False

# S-Bahn

def _get_next_train_minutes() -> Optional[int]:
    if not (DB_CLIENT_ID and DB_CLIENT_SECRET):
        return None
    headers = {"DB-Client-Id": DB_CLIENT_ID, "DB-Api-Key": DB_CLIENT_SECRET, "accept": "application/xml"}
    now = datetime.now(); all_stops = []
    for i in range(2):
        t = now + timedelta(hours=i); date, hour = t.strftime('%y%m%d'), t.strftime('%H')
        try:
            url = f"{DB_API_URL}/{S_BAHN_EVA}/{date}/{hour}"; r = requests.get(url, headers=headers, timeout=API_TIMEOUT)
            r.raise_for_status();
            if not r.content: continue
            root = ET.fromstring(r.content); all_stops.extend(root.findall('s'))
        except Exception:
            return None
    if not all_stops: return None
    upcoming = []
    for s in all_stops:
        try:
            dp = s.find('.//dp');
            if dp is None: continue
            path = dp.get('ppth'); raw = dp.get('pt')
            if not path or not raw: continue
            dest = path.split('|')[-1]
            if dest in OUTBOUND_DESTINATIONS: continue
            dt = datetime.strptime(raw, '%y%m%d%H%M')
            if dt < now: continue
            upcoming.append(int((dt - now).total_seconds()/60))
        except Exception: continue
    return min(upcoming) if upcoming else None

def s_bahn_monitor(controller):
    if not (DB_CLIENT_ID and DB_CLIENT_SECRET):
        logging.warning("S-Bahn disabled: credentials missing")
        return
    while controller.state.running:
        mins = _get_next_train_minutes()
        with controller.state.lock:
            controller.state.s_bahn_minutes_away = mins if mins is not None else -1
        sleep(S_BAHN_POLL_INTERVAL)

# Public-transport commute (transport.rest - free DB HAFAS mirror, no API key)

def _commute_route() -> Optional[dict]:
    for route in TRAFFIC_ROUTES:
        if route.get('name') == 'commute':
            return route
    return TRAFFIC_ROUTES[0] if TRAFFIC_ROUTES else None

def _fetch_transit_commute() -> Dict:
    route = _commute_route()
    if not route:
        return {}
    origin_coord = _tomtom_geocode(route['origin'])
    dest_coord = _tomtom_geocode(route['destination'])
    if not origin_coord or not dest_coord:
        return {}
    try:
        params = {
            'originLatitude': origin_coord[0],
            'originLongitude': origin_coord[1],
            'destinationLatitude': dest_coord[0],
            'destinationLongitude': dest_coord[1],
            'routingDateTimeIsArrival': 'false',
            'transportTypes': 'SCHIFF,UBAHN,TRAM,BUS,SBAHN,REGIONAL_BUS,BAHN',
        }
        r = requests.get(MVG_ROUTES_URL, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        routes_data = r.json() or []
        if not routes_data:
            return {}
        best = routes_data[0]
        parts = best.get('parts') or []
        if not parts:
            return {}
        dep_raw = (parts[0].get('from') or {}).get('plannedDeparture')
        arr_raw = (parts[-1].get('to') or {}).get('plannedDeparture')
        # MVG uses plannedDeparture on the 'to' node of the last part as arrival time.
        if not dep_raw or not arr_raw:
            return {}
        dep = datetime.fromisoformat(dep_raw.replace('Z', '+00:00'))
        arr = datetime.fromisoformat(arr_raw.replace('Z', '+00:00'))
        minutes = max(0, int(round((arr - dep).total_seconds() / 60.0)))
        # Count transport legs (ignore walking/PEDESTRIAN).
        pt_parts = [p for p in parts if ((p.get('line') or {}).get('transportType') or '').upper() != 'PEDESTRIAN']
        transfers = max(0, len(pt_parts) - 1)
        return {
            'commute_minutes': minutes,
            'transfers': transfers,
        }
    except Exception as e:
        logging.debug(f"MVG transit fetch failed: {e}")
        return {}

def transit_monitor(controller):
    if not TOMTOM_API_KEY:
        logging.warning("Transit commute disabled: TOMTOM_API_KEY missing (used for geocoding)")
        return
    while controller.state.running:
        status = _fetch_transit_commute()
        with controller.state.lock:
            controller.state.s_bahn_transit_status = status
        sleep(TRANSIT_POLL_INTERVAL)

# Weather

def _fetch_weather() -> Dict:
    if not OWM_API_KEY: return {}
    url = WEATHER_API_URL_TEMPLATE.format(lat=WEATHER_LAT, lon=WEATHER_LON, api_key=OWM_API_KEY)
    try:
        r = requests.get(url, timeout=API_TIMEOUT); r.raise_for_status(); data = r.json()
        temp = data.get('main', {}).get('temp'); cond = data.get('weather', [{}])[0].get('main')
        if temp is None or not cond: return {}
        return {'temp': temp, 'condition': cond}
    except Exception: return {}

def weather_monitor(controller):
    if not OWM_API_KEY: logging.warning("Weather disabled: OWM_API_KEY missing"); return
    while controller.state.running:
        status = _fetch_weather()
        with controller.state.lock:
            controller.state.weather_status = status
        sleep(WEATHER_POLL_INTERVAL)

# Space Weather

def _fetch_space() -> Dict:
    try:
        r = requests.get(SPACE_WEATHER_URL, timeout=API_TIMEOUT); r.raise_for_status(); data = r.json()
        if not data:
            return {}
        latest = data[-1]
        # NOAA has returned both list-of-lists ([time, kp, ...]) and list-of-dicts
        # ({"time_tag":..., "Kp":...}) formats. Handle both.
        if isinstance(latest, dict):
            raw_kp = latest.get('Kp', latest.get('kp_index'))
        else:
            raw_kp = latest[1]
        if raw_kp is None:
            return {}
        kp = int(float(raw_kp))
        cond = 'Storm' if kp >= 5 else ('Active' if kp == 4 else 'Quiet')
        return {'kp_index': kp, 'condition': cond}
    except Exception as e:
        logging.debug(f"Space weather fetch failed: {e}")
        return {}

def space_weather_monitor(controller):
    while controller.state.running:
        status = _fetch_space()
        with controller.state.lock:
            controller.state.space_weather_status = status
        sleep(SPACE_POLL_INTERVAL)

# Traffic

def _format_minutes(seconds: float) -> str:
    mins = int(round(seconds / 60.0))
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h} h {m} min"

def _tomtom_geocode(address: str) -> Optional[Tuple[float, float]]:
    cached = _TOMTOM_GEOCODE_CACHE.get(address)
    if cached is not None:
        return cached
    try:
        from urllib.parse import quote
        url = TOMTOM_GEOCODE_URL.format(query=quote(address))
        r = requests.get(url, params={'key': TOMTOM_API_KEY, 'limit': 1}, timeout=API_TIMEOUT)
        r.raise_for_status()
        results = r.json().get('results') or []
        if not results:
            return None
        pos = results[0].get('position') or {}
        lat = pos.get('lat'); lon = pos.get('lon')
        if lat is None or lon is None:
            return None
        coord = (float(lat), float(lon))
        _TOMTOM_GEOCODE_CACHE[address] = coord
        return coord
    except Exception as e:
        logging.debug(f"TomTom geocode failed for {address!r}: {e}")
        return None

def _fetch_traffic_tomtom() -> Dict:
    if not TOMTOM_API_KEY:
        return {}
    delays: List[float] = []
    commute_text = 'N/A'
    for route in TRAFFIC_ROUTES:
        origin = _tomtom_geocode(route['origin'])
        dest = _tomtom_geocode(route['destination'])
        if not origin or not dest:
            continue
        loc = f"{origin[0]},{origin[1]}:{dest[0]},{dest[1]}"
        try:
            url = TOMTOM_ROUTING_URL.format(loc=loc)
            params = {
                'key': TOMTOM_API_KEY,
                'traffic': 'true',
                'travelMode': 'car',
                'routeType': 'fastest',
                'computeTravelTimeFor': 'all',
            }
            r = requests.get(url, params=params, timeout=API_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            routes = data.get('routes') or []
            if not routes:
                continue
            summary = routes[0].get('summary') or {}
            # travelTimeInSeconds already reflects live traffic;
            # noTrafficTravelTimeInSeconds is the free-flow baseline.
            traffic_s = summary.get('travelTimeInSeconds')
            base_s = summary.get('noTrafficTravelTimeInSeconds') or summary.get('historicTrafficTravelTimeInSeconds')
            if not traffic_s or not base_s:
                continue
            if base_s > 0:
                delays.append(((traffic_s - base_s) / base_s) * 100.0)
            if route['name'] == 'commute':
                commute_text = _format_minutes(traffic_s)
        except Exception as e:
            logging.debug(f"TomTom routing failed for {route['name']}: {e}")
            continue
    if delays:
        return {'avg_delay': sum(delays) / len(delays), 'commute_time': commute_text}
    return {}

def _fetch_traffic_google() -> Dict:
    if not GOOGLE_MAPS_API_KEY:
        return {}
    delays = []; commute_text = 'N/A'
    for route in TRAFFIC_ROUTES:
        try:
            params = { 'origin': route['origin'], 'destination': route['destination'], 'key': GOOGLE_MAPS_API_KEY, 'departure_time': 'now' }
            r = requests.get(GOOGLE_DIRECTIONS_URL, params=params, timeout=API_TIMEOUT); r.raise_for_status(); data = r.json()
            if data.get('status') != 'OK': continue
            leg = data['routes'][0]['legs'][0]; base = leg['duration']['value']; traffic_val = leg.get('duration_in_traffic', leg['duration'])['value']
            if base > 0: delays.append(((traffic_val - base)/base)*100)
            if route['name'] == 'commute': commute_text = leg.get('duration_in_traffic', leg['duration'])['text']
        except Exception: continue
    if delays: return {'avg_delay': sum(delays)/len(delays), 'commute_time': commute_text}
    return {}

def _fetch_traffic() -> Dict:
    # Prefer TomTom (free tier); fall back to Google if still configured.
    if TOMTOM_API_KEY:
        result = _fetch_traffic_tomtom()
        if result:
            return result
    return _fetch_traffic_google()

def traffic_monitor(controller):
    if not (TOMTOM_API_KEY or GOOGLE_MAPS_API_KEY):
        logging.warning("Traffic disabled: set TOMTOM_API_KEY (free) or GOOGLE_MAPS_API_KEY")
        return
    while controller.state.running:
        status = _fetch_traffic()
        with controller.state.lock:
            controller.state.traffic_status = status
        sleep(TRAFFIC_POLL_INTERVAL)

# Environment (UV, pollen, air quality, sun) via Open-Meteo (free, no key)

def _level_from_thresholds(value: Optional[float], thresholds: Tuple[float, float]) -> str:
    if value is None:
        return 'unknown'
    low, high = thresholds
    if value < low:
        return 'low'
    if value < high:
        return 'moderate'
    return 'high'

def _fetch_environment() -> Dict:
    result: Dict = {}
    # Forecast: UV index + is_day + sunrise/sunset
    try:
        r = requests.get(
            OPEN_METEO_FORECAST_URL,
            params={
                'latitude': WEATHER_LAT,
                'longitude': WEATHER_LON,
                'current': 'uv_index,is_day',
                'daily': 'sunrise,sunset',
                'timezone': 'auto',
                'forecast_days': 1,
            },
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        cur = data.get('current') or {}
        uv = cur.get('uv_index')
        if isinstance(uv, (int, float)):
            result['uv_index'] = round(float(uv), 1)
        is_day = cur.get('is_day')
        if is_day is not None:
            result['is_day'] = bool(is_day)
        daily = data.get('daily') or {}
        sunrises = daily.get('sunrise') or []
        sunsets = daily.get('sunset') or []
        if sunrises:
            result['sunrise'] = sunrises[0]
        if sunsets:
            result['sunset'] = sunsets[0]
    except Exception as e:
        logging.warning(f"Open-Meteo forecast fetch failed: {e}")

    # Air quality + pollen
    try:
        r = requests.get(
            OPEN_METEO_AIR_QUALITY_URL,
            params={
                'latitude': WEATHER_LAT,
                'longitude': WEATHER_LON,
                'current': 'european_aqi,pm2_5,grass_pollen,birch_pollen,alder_pollen,olive_pollen,mugwort_pollen,ragweed_pollen',
                'timezone': 'auto',
            },
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        cur = data.get('current') or {}
        aqi = cur.get('european_aqi')
        if isinstance(aqi, (int, float)):
            result['aqi'] = int(round(aqi))
            # EU AQI bands: 0-20 good, 20-40 fair, 40-60 moderate, 60-80 poor, 80-100 very poor, 100+ extreme.
            if aqi < 20: result['aqi_label'] = 'good'
            elif aqi < 40: result['aqi_label'] = 'fair'
            elif aqi < 60: result['aqi_label'] = 'moderate'
            elif aqi < 80: result['aqi_label'] = 'poor'
            else: result['aqi_label'] = 'very poor'

        pollen_species = ('grass_pollen', 'birch_pollen', 'alder_pollen',
                          'olive_pollen', 'mugwort_pollen', 'ragweed_pollen')
        max_pollen: Optional[float] = None
        dominant: Optional[str] = None
        for sp in pollen_species:
            v = cur.get(sp)
            if isinstance(v, (int, float)):
                if max_pollen is None or v > max_pollen:
                    max_pollen = float(v)
                    dominant = sp.replace('_pollen', '')
        if max_pollen is not None:
            # Rough bands (grains/m³) used by many European networks.
            result['pollen_max'] = round(max_pollen, 1)
            result['pollen_level'] = _level_from_thresholds(max_pollen, (20.0, 50.0))
            if dominant:
                result['pollen_dominant'] = dominant
    except Exception as e:
        logging.warning(f"Open-Meteo air quality fetch failed: {e}")

    return result

def environment_monitor(controller):
    while controller.state.running:
        status = _fetch_environment()
        with controller.state.lock:
            controller.state.environment_status = status
        sleep(ENVIRONMENT_POLL_INTERVAL)

# Grêmio last match result and next match via ESPN free API (no key required)

def _espn_competitor_score(competitor: Dict) -> int:
    s = competitor.get('score')
    if isinstance(s, dict):
        return int(float(s.get('displayValue') or 0))
    return int(float(s or 0))

def _fetch_gremio_last_result() -> Dict:
    out: Dict = {}

    # Last completed match from team schedule (events newest-first)
    try:
        r = requests.get(GREMIO_ESPN_SCHEDULE_URL, timeout=API_TIMEOUT)
        r.raise_for_status()
        for ev in r.json().get('events') or []:
            comp = (ev.get('competitions') or [{}])[0]
            if not (comp.get('status') or {}).get('type', {}).get('completed'):
                continue
            competitors = comp.get('competitors') or []
            gremio_c = next((c for c in competitors if 'grêmio' in (c.get('team', {}).get('displayName') or '').lower()), None)
            opp_c = next((c for c in competitors if c is not gremio_c), None)
            if not gremio_c or not opp_c:
                continue
            gs = _espn_competitor_score(gremio_c)
            os_ = _espn_competitor_score(opp_c)
            if gremio_c.get('winner'):
                match_result = 'win'
            elif gs == os_:
                match_result = 'draw'
            else:
                match_result = 'loss'
            out.update({
                'result': match_result,
                'score': f"{gs}-{os_}",
                'opponent': (opp_c.get('team') or {}).get('displayName', '?'),
                'home_away': 'home' if gremio_c.get('homeAway') == 'home' else 'away',
                'date': (ev.get('date') or '')[:10],
                'competition': (ev.get('league') or {}).get('name'),
            })
            break
    except Exception as e:
        logging.debug(f"Grêmio ESPN schedule fetch failed: {e}")

    # Next upcoming match (search 60 days ahead on the scoreboard)
    try:
        today = datetime.utcnow()
        end_dt = today + timedelta(days=60)
        date_range = f"{today.strftime('%Y%m%d')}-{end_dt.strftime('%Y%m%d')}"
        r = requests.get(GREMIO_ESPN_SCOREBOARD_URL, params={'dates': date_range}, timeout=API_TIMEOUT)
        r.raise_for_status()
        for ev in r.json().get('events') or []:
            comp = (ev.get('competitions') or [{}])[0]
            competitors = comp.get('competitors') or []
            gremio_c = next((c for c in competitors if 'grêmio' in (c.get('team', {}).get('displayName') or '').lower()), None)
            if not gremio_c:
                continue
            opp_c = next((c for c in competitors if c is not gremio_c), None)
            out['next_match_date'] = ev.get('date', '')
            out['next_match_opponent'] = (opp_c.get('team') or {}).get('displayName', '?') if opp_c else '?'
            out['next_match_home_away'] = 'home' if gremio_c.get('homeAway') == 'home' else 'away'
            break
    except Exception as e:
        logging.debug(f"Grêmio ESPN next match fetch failed: {e}")

    return out

def gremio_monitor(controller):
    while controller.state.running:
        status = _fetch_gremio_last_result()
        with controller.state.lock:
            controller.state.gremio_status = status
        sleep(GREMIO_POLL_INTERVAL)

def audio_vu_monitor(controller):
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        logging.warning("Audio VU disabled: install sounddevice and numpy")
        return

    blocksize = 256

    def _resolve_input_device() -> Tuple[Optional[int], Optional[str]]:
        try:
            devices = sd.query_devices()
        except Exception as e:
            logging.error(f"Audio VU: failed to query devices: {e}")
            return None, None

        def _is_valid_input(idx: int) -> bool:
            if idx < 0 or idx >= len(devices):
                return False
            d = devices[idx]
            return int(d.get('max_input_channels', 0)) > 0

        configured = AUDIO_INPUT_DEVICE
        if configured:
            if configured.isdigit():
                idx = int(configured)
                if _is_valid_input(idx):
                    return idx, str(devices[idx].get('name', f'device-{idx}'))
                logging.warning(f"Audio VU: AUDIO_INPUT_DEVICE index {idx} is not a valid input device")
            else:
                wanted = configured.lower()
                for idx, dev in enumerate(devices):
                    name = str(dev.get('name', ''))
                    if wanted in name.lower() and _is_valid_input(idx):
                        return idx, name
                logging.warning(f"Audio VU: AUDIO_INPUT_DEVICE '{configured}' not found among input devices")

        try:
            default_in = sd.default.device[0]
            if isinstance(default_in, int) and _is_valid_input(default_in):
                return default_in, str(devices[default_in].get('name', f'device-{default_in}'))
        except Exception:
            pass

        preferred_terms = ('usb', 'mic', 'microphone', 'audio')
        for idx, dev in enumerate(devices):
            name = str(dev.get('name', ''))
            lname = name.lower()
            if _is_valid_input(idx) and any(t in lname for t in preferred_terms):
                return idx, name

        for idx in range(len(devices)):
            if _is_valid_input(idx):
                return idx, str(devices[idx].get('name', f'device-{idx}'))

        return None, None

    def _update_level(level: float) -> None:
        raw = min((level / AUDIO_VU_REFERENCE_LEVEL), 1.0)
        now = time()
        with controller.state.lock:
            baseline = float(controller.state.mode_state.get('audio_vu_baseline', raw) or raw)
            baseline = (baseline * 0.985) + (raw * 0.015)
            controller.state.mode_state['audio_vu_baseline'] = baseline

            onset = max(0.0, raw - baseline)
            last_beat = float(controller.state.mode_state.get('audio_vu_last_beat', 0.0) or 0.0)
            beat_hold_until = float(controller.state.mode_state.get('audio_vu_beat_hold_until', 0.0) or 0.0)

            is_beat = (
                raw >= AUDIO_VU_LOUD_GATE
                and onset >= AUDIO_VU_BEAT_DELTA
                and (now - last_beat) >= AUDIO_VU_BEAT_MIN_INTERVAL
            )
            if is_beat:
                controller.state.mode_state['audio_vu_last_beat'] = now
                beat_hold_until = now + AUDIO_VU_BEAT_HOLD_SECONDS
                controller.state.mode_state['audio_vu_beat_hold_until'] = beat_hold_until

            if now < beat_hold_until:
                target = max(raw, min(1.0, 0.52 + onset * 2.2))
            else:
                target = raw if raw >= AUDIO_VU_LOUD_GATE else 0.0

            prev = controller.state.audio_vu_level
            smoothing = AUDIO_VU_SMOOTHING_ATTACK if target >= prev else AUDIO_VU_SMOOTHING_RELEASE
            controller.state.audio_vu_level = max(0.0, (prev * smoothing) + (target * (1.0 - smoothing)))
            controller.state.mode_state['audio_vu_last'] = now

    device_index, device_name = _resolve_input_device()
    if device_index is None:
        logging.warning("Audio VU disabled: no input-capable audio device found")
        return

    def _candidate_sample_rates() -> List[int]:
        candidates: List[int] = []
        try:
            dev_info = sd.query_devices(device_index)
            default_rate = int(round(float(dev_info.get('default_samplerate', 0))))
            if default_rate > 0:
                candidates.append(default_rate)
        except Exception:
            pass

        for rate in (48000, 44100, 32000, 24000, 22050, 16000, 11025, 8000):
            if rate not in candidates:
                candidates.append(rate)
        return candidates

    opened = False
    last_error: Optional[Exception] = None
    tried_rates = _candidate_sample_rates()

    try:
        for samplerate in tried_rates:
            try:
                with sd.InputStream(
                    device=device_index,
                    channels=1,
                    samplerate=samplerate,
                    blocksize=blocksize,
                    dtype='float32',
                    latency='low',
                ) as stream:
                    opened = True
                    logging.info(
                        f"Audio VU monitor started (device={device_index}: {device_name}, rate={samplerate}Hz)"
                    )
                    while controller.state.running:
                        data, overflowed = stream.read(blocksize)
                        if overflowed:
                            logging.debug(f"Audio input overflow: {overflowed}")
                        if data.size == 0:
                            continue
                        samples = data[:, 0] if data.ndim > 1 else data
                        rms = float(np.sqrt(np.mean(np.square(samples))))
                        peak = float(np.max(np.abs(samples)))
                        level = max(rms, peak * 0.35)
                        _update_level(level)
                    break
            except Exception as e:
                last_error = e
                logging.warning(
                    f"Audio VU: failed opening InputStream at {samplerate}Hz on device {device_index} ({device_name}): {e}"
                )

        if not opened:
            logging.error(
                f"Audio VU monitor error: unable to open InputStream on device {device_index} ({device_name}). "
                f"Tried rates={tried_rates}. Last error: {last_error}"
            )
    finally:
        with controller.state.lock:
            controller.state.audio_vu_level = 0.0

# iRacing UDP

def network_monitor(controller):
    prev: Optional[bool] = None
    while controller.state.running:
        try:
            connected = _is_network_connected()
        except Exception:
            connected = False
        with controller.state.lock:
            controller.state.network_connected = connected
            if connected != prev:
                if connected:
                    controller.state.target_mode = 'manual'
                    controller.state.target_manual_color = 'off'
                    logging.info("Network up -> target manual/off")
                else:
                    controller.state.target_mode = 'auto'
                    logging.info("Network down -> target auto")
                prev = connected
        sleep(NETWORK_POLL_INTERVAL)

def iracing_udp_listener(controller):
    valid = {'red','yellow','green','black','green-yellow','all_on'}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((IRACING_UDP_HOST, IRACING_UDP_PORT)); sock.settimeout(1.0)
            while controller.state.running:
                try:
                    data, _ = sock.recvfrom(1024)
                    payload = data.decode('utf-8').strip()
                    # Wire format is "<color>" or "<color>:<revpct>"; the rev
                    # percentage is optional so older senders keep working.
                    color, _, rev_raw = payload.partition(':')
                    if color in valid:
                        rev_pct = None
                        if rev_raw:
                            try:
                                rev_pct = max(0.0, min(100.0, float(rev_raw)))
                            except ValueError:
                                rev_pct = None
                        with controller.state.lock:
                            controller.state.iracing_light_status = color
                            controller.state.iracing_rev_pct = rev_pct
                except socket.timeout: continue
                except Exception: continue
    except Exception as e:
        logging.error(f"iRacing listener error: {e}")

# --------------------------- Web Server ------------------------------------
_HTML = f"""
    <!DOCTYPE html><html lang="en"><head><title>Traffic Light Control</title><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover"><meta name="theme-color" content="#0d0f13"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Traffic Light">
    <style>
    :root{{--page-bg-1:#0d0f13;--page-bg-2:#161a22;--panel:rgba(26,29,35,.72);--panel-border:rgba(255,255,255,.08);--text-color:#eef0f2;--text-muted:#8b909a;--accent-color:#2f9bff;--accent-glow:rgba(47,155,255,.35);--ok:#00e08a;--warn:#ffb020;--shadow-color:rgba(0,0,0,.45)}}
    *{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
    html,body{{height:100%;margin:0;padding:0;background:var(--page-bg-1)}}
    body{{min-height:100dvh;background:radial-gradient(1200px 600px at 50% -10%,rgba(47,155,255,.10),transparent 60%),linear-gradient(180deg,var(--page-bg-1),var(--page-bg-2));font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:var(--text-color);display:flex;justify-content:center;padding:max(env(safe-area-inset-top),20px) 16px max(env(safe-area-inset-bottom),24px)}}
    .container{{width:100%;max-width:380px;display:flex;flex-direction:column;gap:20px}}
    .topbar{{display:flex;align-items:center;justify-content:space-between;padding:2px 4px}}
    .brand{{font-weight:700;font-size:1.05em;letter-spacing:-.01em}}
    .conn-pill{{display:flex;align-items:center;gap:6px;font-size:.78em;font-weight:600;color:var(--text-muted);background:rgba(255,255,255,.06);border:1px solid var(--panel-border);border-radius:999px;padding:5px 11px 5px 8px;transition:background .25s,color .25s}}
    .conn-dot{{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 6px var(--ok)}}
    .conn-pill.live{{color:#bfe9d6}}
    .conn-pill.lost{{color:#ffd9a0}}
    .conn-pill.lost .conn-dot{{background:var(--warn);box-shadow:0 0 6px var(--warn);animation:pulse 1s infinite}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .glass-card{{background:var(--panel);backdrop-filter:blur(20px) saturate(160%);-webkit-backdrop-filter:blur(20px) saturate(160%);border:1px solid var(--panel-border);border-radius:28px;padding:26px 22px 22px;box-shadow:0 20px 50px var(--shadow-color),inset 0 1px 0 rgba(255,255,255,.05);display:flex;flex-direction:column;align-items:center;gap:16px}}
    .traffic-light-body{{position:relative;background:linear-gradient(180deg,#181c24,#111318);border-radius:26px;padding:20px 22px;display:flex;flex-direction:column;gap:14px;border:1px solid rgba(255,255,255,.06);box-shadow:inset 0 2px 6px rgba(0,0,0,.5),0 6px 18px rgba(0,0,0,.4)}}
    .light{{position:relative;width:86px;height:86px;border-radius:50%;background:#2a2d33;opacity:.4;transition:all .18s cubic-bezier(.4,0,.2,1);cursor:pointer;touch-action:manipulation;box-shadow:inset 0 2px 10px rgba(0,0,0,.5)}}
    .red-on{{background:#ff2b2b;opacity:1;box-shadow:0 0 36px 4px rgba(255,43,43,.55),inset 0 2px 10px rgba(0,0,0,.4)}}
    .yellow-on{{background:#ffcb2e;opacity:1;box-shadow:0 0 36px 4px rgba(255,203,46,.5),inset 0 2px 10px rgba(0,0,0,.4)}}
    .green-on{{background:#2bd672;opacity:1;box-shadow:0 0 36px 4px rgba(43,214,114,.5),inset 0 2px 10px rgba(0,0,0,.4)}}
    .controls{{text-align:center;width:100%}}
    #modeText{{font-size:.72em;font-weight:700;letter-spacing:.12em;color:var(--text-muted);text-transform:uppercase;margin:0}}
    #modeText strong{{display:block;font-size:1.6em;letter-spacing:0;color:var(--text-color);margin-top:3px}}
    .info-text{{height:2.7em;line-height:1.35em;font-size:.92em;color:var(--text-muted);margin-top:8px;overflow:hidden}}
    .section-label{{font-size:.72em;font-weight:700;letter-spacing:.1em;color:var(--text-muted);text-transform:uppercase;margin:0 4px}}
    .mode-buttons{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;width:100%}}
    .mode-buttons a{{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 4px 10px;background:rgba(255,255,255,.045);border:1px solid var(--panel-border);border-radius:16px;color:var(--text-muted);text-decoration:none;touch-action:manipulation;transition:background .15s,transform .08s,color .15s,border-color .15s}}
    .mode-buttons a:active{{transform:scale(.94)}}
    .mode-buttons a svg{{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}}
    .mode-buttons a span{{font-size:.68em;font-weight:600;letter-spacing:.01em}}
    .mode-buttons a.active{{background:linear-gradient(180deg,rgba(47,155,255,.28),rgba(47,155,255,.14));border-color:rgba(47,155,255,.55);color:#fff;box-shadow:0 0 0 1px rgba(47,155,255,.25),0 4px 14px var(--accent-glow)}}
    .stage{{position:relative;width:fit-content;margin:0 auto}}
    .led-strip{{position:absolute;top:0;bottom:0;left:100%;margin-left:14px;width:26px;display:flex;flex-direction:column-reverse;gap:4px;padding:8px 6px;background:rgba(0,0,0,.35);border-radius:10px;border:1px solid var(--panel-border);opacity:0;pointer-events:none;transition:opacity .15s}}
    .led-strip.visible{{opacity:1}}
    .led-seg{{flex:1 1 0;border-radius:3px;background:#2a2d33;transition:all .12s}}
    .led-seg.lit{{box-shadow:0 0 6px var(--glow)}}
    .gremio-stars{{position:absolute;top:-16px;left:50%;transform:translateX(-50%);display:none;gap:8px;align-items:center;justify-content:center;pointer-events:none}}
    .gremio-stars.visible{{display:flex}}
    .gremio-stars svg{{width:12px;height:12px}}
    .traffic-light-body.gremio-theme{{background:#1F1A17;border-color:#0D80BF}}
    .light.gremio-win-on{{background-color:#0D80BF;opacity:1;box-shadow:0 0 40px #0D80BF,inset 0 2px 10px rgba(0,0,0,.4)}}
    .gremio-face{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:60%;height:60%;opacity:0;pointer-events:none;transition:opacity .15s}}
    .gremio-face.visible{{opacity:1}}
    </style></head>
    <body><div class="container">
    <div class="topbar"><div class="brand">Traffic Light</div><div class="conn-pill live" id="connPill"><div class="conn-dot"></div><span id="connLabel">Live</span></div></div>
    <div class="glass-card">
    <div class="stage"><div class="gremio-stars" id="gremioStars"><svg viewBox="0 0 24 24" fill="#93754F"><path d="M12 2l2.9 6.6L22 9.3l-5 4.8 1.3 7.1L12 17.8 5.7 21.2 7 14.1 2 9.3l7.1-.7z"/></svg><svg viewBox="0 0 24 24" fill="#ABAAAA"><path d="M12 2l2.9 6.6L22 9.3l-5 4.8 1.3 7.1L12 17.8 5.7 21.2 7 14.1 2 9.3l7.1-.7z"/></svg><svg viewBox="0 0 24 24" fill="#F8C300"><path d="M12 2l2.9 6.6L22 9.3l-5 4.8 1.3 7.1L12 17.8 5.7 21.2 7 14.1 2 9.3l7.1-.7z"/></svg></div><div class="traffic-light-body" id="traffic-light"><div id="red" class="light" onclick="handleLightClick('red')"><svg class="gremio-face" id="gremioFaceRed" viewBox="0 0 40 40"></svg></div><div id="yellow" class="light" onclick="handleLightClick('yellow')"><svg class="gremio-face" id="gremioFaceYellow" viewBox="0 0 40 40"></svg></div><div id="green" class="light" onclick="handleLightClick('green')"><svg class="gremio-face" id="gremioFaceGreen" viewBox="0 0 40 40"></svg></div></div><div class="led-strip" id="ledStrip"></div></div>
    <div class="controls"><h2 id="modeText">Current Mode: <strong></strong></h2><div id="info-display" class="info-text"></div></div>
    </div>
    <div class="section-label">Modes</div>
    <div class="mode-buttons">
    <a href="#" id="mode-auto" onclick="event.preventDefault(); handleModeClick('auto')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg><span>Auto</span></a>
    <a href="#" id="mode-emergency" onclick="event.preventDefault(); handleModeClick('emergency')"><svg viewBox="0 0 24 24"><path d="M13 2 4 14h6l-1 8 9-12h-6z"/></svg><span>Emergency</span></a>
    <a href="#" id="mode-sos" onclick="event.preventDefault(); handleModeClick('sos')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 9v6M12 9v6M16 9v6"/></svg><span>SOS</span></a>
    <a href="#" id="mode-s_bahn" onclick="event.preventDefault(); handleModeClick('s_bahn')"><svg viewBox="0 0 24 24"><rect x="5" y="4" width="14" height="14" rx="4"/><path d="M5 14h14M9 18l-2 3M15 18l2 3"/><circle cx="9" cy="10" r="1"/><circle cx="15" cy="10" r="1"/></svg><span>S-Bahn</span></a>
    <a href="#" id="mode-stau" onclick="event.preventDefault(); handleModeClick('stau')"><svg viewBox="0 0 24 24"><path d="M4 16v-3l2-4h12l2 4v3"/><path d="M4 16h16M7 16v2M17 16v2"/><circle cx="7.5" cy="16.5" r=".5"/><circle cx="16.5" cy="16.5" r=".5"/></svg><span>Stau</span></a>
    <a href="#" id="mode-biergarten" onclick="event.preventDefault(); handleModeClick('biergarten')"><svg viewBox="0 0 24 24"><path d="M6 8h9v9a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3z"/><path d="M15 10h2a2 2 0 0 1 0 4h-2"/><path d="M6 8l-1-4M11 8l1-4"/></svg><span>Biergarten</span></a>
    <a href="#" id="mode-uv" onclick="event.preventDefault(); handleModeClick('uv')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M3 12h2M19 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"/></svg><span>UV</span></a>
    <a href="#" id="mode-space" onclick="event.preventDefault(); handleModeClick('space')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><ellipse cx="12" cy="12" rx="9" ry="3.2" transform="rotate(35 12 12)"/></svg><span>Space</span></a>
    <a href="#" id="mode-party" onclick="event.preventDefault(); handleModeClick('party')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2"/></svg><span>Party</span></a>
    <a href="#" id="mode-racing" onclick="event.preventDefault(); handleModeClick('racing')"><svg viewBox="0 0 24 24"><path d="M6 3v18"/><path d="M6 4h11l-2 3 2 3H6"/></svg><span>Racing</span></a>
    <a href="#" id="mode-audio_vu" onclick="event.preventDefault(); handleModeClick('audio_vu')"><svg viewBox="0 0 24 24"><path d="M4 12v2M8 9v8M12 5v16M16 9v8M20 12v2"/></svg><span>Audio VU</span></a>
    <a href="#" id="mode-gremio" onclick="event.preventDefault(); handleModeClick('gremio')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3.5 9 Q12 12.2 20.5 9"/><path d="M3.5 15 Q12 11.8 20.5 15"/></svg><span>Grêmio</span></a>
    </div>
    </div>
    <script>
        let currentModeFromServer = 'unknown'; let localAnimationId = null;
        const LED_SEGMENTS = Array.from({{length: 10}}, () => {{
            const seg = document.createElement('div');
            seg.className = 'led-seg';
            document.getElementById('ledStrip').appendChild(seg);
            return seg;
        }});
        function barHexForColor(color) {{
            if (color === 'red' || color === 'all_on') return '#ff1c1c';
            if (color === 'yellow' || color === 'green-yellow') return '#ffc700';
            if (color === 'green') return '#00ff00';
            return null;
        }}
        function computeBarPct(mode, s_bahn_minutes, space_weather, traffic, audio_level, environment, iracing_rev_pct) {{
            if (mode === 'racing') {{
                if (iracing_rev_pct === undefined || iracing_rev_pct === null) return null;
                return Math.max(0, Math.min(1, iracing_rev_pct / 100));
            }}
            if (mode === 'uv') {{
                const v = (environment || {{}}).uv_index;
                return (v === undefined || v === null) ? 1 : Math.max(0, Math.min(1, v / 11));
            }}
            if (mode === 'space') {{
                const v = (space_weather || {{}}).kp_index;
                return (v === undefined || v === null) ? 1 : Math.max(0, Math.min(1, v / 9));
            }}
            if (mode === 'stau') {{
                const v = (traffic || {{}}).avg_delay;
                return (v === undefined || v === null) ? 1 : Math.max(0, Math.min(1, v / 60));
            }}
            if (mode === 'audio_vu') {{
                return Math.max(0, Math.min(1, audio_level || 0));
            }}
            if (mode === 's_bahn') {{
                if (s_bahn_minutes === -1) return 1;
                return Math.max(0, Math.min(1, 1 - Math.max(0, Math.min(20, s_bahn_minutes)) / 20));
            }}
            return null;
        }}
        function updateBar(color, mode, s_bahn_minutes, space_weather, traffic, audio_level, environment, iracing_rev_pct) {{
            const ledStrip = document.getElementById('ledStrip');
            const pct = computeBarPct(mode, s_bahn_minutes, space_weather, traffic, audio_level, environment, iracing_rev_pct);
            if (pct === null) {{
                ledStrip.classList.remove('visible');
                return;
            }}
            ledStrip.classList.add('visible');
            // In racing mode the bar tracks rev_pct continuously even while the main
            // lights are still black (below the first shift point) - fall back to green.
            const hex = barHexForColor(color) || (mode === 'racing' ? '#00ff00' : null);
            const raw = hex ? pct * LED_SEGMENTS.length : 0;
            const fullLit = Math.floor(raw);
            const frac = raw - fullLit;
            LED_SEGMENTS.forEach((seg, i) => {{
                const level = i < fullLit ? 1 : (i === fullLit ? frac : 0);
                const lit = level > 0.03;
                seg.classList.toggle('lit', lit);
                seg.style.background = lit ? hex : '#2a2d33';
                seg.style.opacity = lit ? (0.35 + 0.65 * level) : 1;
                seg.style.setProperty('--glow', lit ? hex : 'transparent');
            }});
        }}
        function updateVisuals(color, mode, s_bahn_minutes, weather, race_step, space_weather, traffic, audio_level, s_bahn_transit, environment, gremio, iracing_rev_pct) {{
            updateBar(color, mode, s_bahn_minutes, space_weather, traffic, audio_level, environment, iracing_rev_pct);
            if (currentModeFromServer !== mode) {{
                const currentActive = document.querySelector('.mode-buttons a.active');
                if (currentActive) currentActive.classList.remove('active');
                if (mode !== 'idle' && mode !== 'manual') {{
                    const newActive = document.getElementById(`mode-${{mode}}`);
                    if (newActive) newActive.classList.add('active');
                }}
            }}
            currentModeFromServer = mode;
            document.querySelector('#modeText strong').textContent = (mode === 'idle') ? 'OFF' : mode.replace('_', ' ').toUpperCase();
            const infoDisplay = document.getElementById('info-display');
            if (mode === 's_bahn') {{
                let base;
                if (s_bahn_minutes === -1) {{ base = 'No S-Bahn data.'; }}
                else {{ base = `Next train in ${{s_bahn_minutes}} min.`; }}
                if (s_bahn_transit && typeof s_bahn_transit.commute_minutes === 'number') {{
                    const xfer = s_bahn_transit.transfers ? ` (${{s_bahn_transit.transfers}} transfer${{s_bahn_transit.transfers === 1 ? '' : 's'}})` : '';
                    base += ` By transit: ${{s_bahn_transit.commute_minutes}} min${{xfer}}.`;
                }}
                infoDisplay.textContent = base;
            }}
            else if (mode === 'biergarten') {{
                if (weather && weather.temp && weather.condition) {{ infoDisplay.textContent = `${{weather.temp.toFixed(1)}}°C, ${{weather.condition}}`; }}
                else {{ infoDisplay.textContent = 'No weather data.'; }}
            }}
            else if (mode === 'racing' && race_step >= 4) {{
                infoDisplay.textContent = (typeof iracing_rev_pct === 'number') ? `Revs: ${{Math.round(iracing_rev_pct)}}%` : 'Listening for iRacing...';
            }}
            else if (mode === 'space') {{
                if (space_weather && space_weather.kp_index !== undefined) {{ infoDisplay.textContent = `Kp-index: ${{space_weather.kp_index}} (${{space_weather.condition}})`; }}
                else {{ infoDisplay.textContent = 'No space weather data.'; }}
            }}
            else if (mode === 'stau') {{
                if (traffic && traffic.commute_time) {{ infoDisplay.textContent = `By car: ${{traffic.commute_time}}`; }}
                else {{ infoDisplay.textContent = 'No traffic data.'; }}
            }}
            else if (mode === 'audio_vu') {{
                if (typeof audio_level === 'number') {{
                    const pct = Math.round(Math.min(Math.max(audio_level, 0), 1) * 100);
                    infoDisplay.textContent = `Audio level: ${{pct}}%`;
                }} else {{
                    infoDisplay.textContent = 'No audio data.';
                }}
            }}
            else if (mode === 'uv') {{
                const e = environment || {{}};
                const w = weather || {{}};
                const parts = [];
                if (typeof e.uv_index === 'number') {{
                    let band = 'low';
                    if (e.uv_index >= 8) band = 'very high';
                    else if (e.uv_index >= 6) band = 'high';
                    else if (e.uv_index >= 3) band = 'moderate';
                    parts.push(`UV ${{e.uv_index.toFixed(1)}} (${{band}})`);
                }} else {{
                    parts.push('UV n/a');
                }}
                if (typeof w.temp === 'number' && w.condition) {{
                    parts.push(`${{w.temp.toFixed(0)}}°C ${{w.condition}}`);
                }}
                if (e.aqi_label) {{
                    parts.push(`Air ${{e.aqi_label}}`);
                }}
                if (e.pollen_level && e.pollen_level !== 'unknown') {{
                    const dom = e.pollen_dominant ? ` (${{e.pollen_dominant}})` : '';
                    parts.push(`Pollen ${{e.pollen_level}}${{dom}}`);
                }}
                if (typeof e.is_day === 'boolean') {{
                    parts.push(e.is_day ? 'Day' : 'Night');
                }}
                infoDisplay.textContent = parts.join(' · ');
            }}
            else if (mode === 'gremio') {{
                const g = gremio || {{}};
                const parts = [];
                const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
                const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                if (g.result && g.score) {{
                    const labels = {{win: 'V', draw: 'E', loss: 'D'}};
                    const tag = labels[g.result] || g.result;
                    const vs = g.home_away === 'home' ? 'vs' : '@';
                    let dateStr = '';
                    if (g.date) {{
                        const d2 = new Date(g.date + 'T12:00Z');
                        dateStr = ` · ${{days[d2.getUTCDay()]}} ${{d2.getUTCDate()}} ${{months[d2.getUTCMonth()]}}`;
                    }}
                    parts.push(`Last: ${{tag}} ${{g.score}} ${{vs}} ${{g.opponent || '?'}}${{dateStr}}`);
                }}
                if (g.next_match_date) {{
                    const d = new Date(g.next_match_date);
                    const hh = d.getUTCHours().toString().padStart(2,'0');
                    const mm = d.getUTCMinutes().toString().padStart(2,'0');
                    const vs2 = g.next_match_home_away === 'home' ? 'vs' : '@';
                    parts.push(`Next: ${{vs2}} ${{g.next_match_opponent || '?'}} · ${{days[d.getUTCDay()]}} ${{d.getUTCDate()}} ${{months[d.getUTCMonth()]}} ${{hh}}:${{mm}}`);
                }}
                if (parts.length) {{
                    infoDisplay.innerHTML = parts.join('<br>');
                }} else {{
                    infoDisplay.textContent = 'No Grêmio data.';
                }}
            }}
            else {{ infoDisplay.textContent = ''; }}
            const isRedOn = color === 'red' || color === 'red_and_yellow' || color === 'all_on';
            const isYellowOn = color === 'yellow' || color === 'red_and_yellow' || color === 'all_on' || color === 'green-yellow';
            const isGreenOn = color === 'green' || color === 'all_on' || color === 'green-yellow';
            document.getElementById('red').className = 'light' + (isRedOn ? ' red-on' : '');
            document.getElementById('yellow').className = 'light' + (isYellowOn ? ' yellow-on' : '');
            document.getElementById('green').className = 'light' + (isGreenOn ? (mode === 'gremio' ? ' gremio-win-on' : ' green-on') : '');

            const tlBody = document.getElementById('traffic-light');
            const gremioStars = document.getElementById('gremioStars');
            const gremioFaces = [document.getElementById('gremioFaceRed'), document.getElementById('gremioFaceYellow'), document.getElementById('gremioFaceGreen')];
            gremioFaces.forEach(f => f.classList.remove('visible'));
            if (mode === 'gremio') {{
                tlBody.classList.add('gremio-theme');
                gremioStars.classList.add('visible');
                const result = (gremio || {{}}).result;
                const EXPRESSIONS = {{
                    happy:   {{mouth:'M8,10 Q20,26 32,10', angry:false}},
                    neutral: {{mouth:'M10,18 L30,18', angry:false}},
                    angry:   {{mouth:'M8,24 Q20,10 32,24', angry:true}},
                }};
                const key = result === 'win' ? 'happy' : result === 'loss' ? 'angry' : (result === 'draw' ? 'neutral' : null);
                const activeFace = isGreenOn ? gremioFaces[2] : isYellowOn ? gremioFaces[1] : isRedOn ? gremioFaces[0] : null;
                if (activeFace && key) {{
                    const e = EXPRESSIONS[key];
                    const eyeY = e.angry ? 16 : 15;
                    const brows = e.angry
                        ? `<line x1="8" y1="10" x2="15" y2="13" stroke="#fff" stroke-width="3" stroke-linecap="round"/><line x1="32" y1="10" x2="25" y2="13" stroke="#fff" stroke-width="3" stroke-linecap="round"/>`
                        : '';
                    activeFace.innerHTML = `${{brows}}<circle cx="12" cy="${{eyeY}}" r="2.6" fill="#fff"/><circle cx="28" cy="${{eyeY}}" r="2.6" fill="#fff"/><path d="${{e.mouth}}" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round"/>`;
                    activeFace.classList.add('visible');
                }}
            }} else {{
                tlBody.classList.remove('gremio-theme');
                gremioStars.classList.remove('visible');
            }}
        }}
        function stopLocalAnimation() {{ if (localAnimationId) {{ clearInterval(localAnimationId); clearTimeout(localAnimationId); localAnimationId = null; }} }}
        function startPartyAnimation() {{ stopLocalAnimation(); localAnimationId = setInterval(() => {{ const colors = ['red', 'yellow', 'green', 'off']; updateVisuals(colors[Math.floor(Math.random() * colors.length)], 'party', -1, {{}}, 0, {{}}, {{}}, 0, {{}}, {{}}, {{}}); }}, 80); }}
        function startSosAnimation() {{
            stopLocalAnimation();
            const sosPattern = [
                {{state: 'all_on', duration: 200}}, {{state: 'off', duration: 200}},{{state: 'all_on', duration: 200}}, {{state: 'off', duration: 200}},{{state: 'all_on', duration: 200}}, {{state: 'off', duration: 400}},
                {{state: 'all_on', duration: 600}}, {{state: 'off', duration: 200}},{{state: 'all_on', duration: 600}}, {{state: 'off', duration: 200}},{{state: 'all_on', duration: 600}}, {{state: 'off', duration: 400}},
                {{state: 'all_on', duration: 200}}, {{state: 'off', duration: 200}},{{state: 'all_on', 'duration': 200}}, {{state: 'off', duration: 200}},{{state: 'all_on', duration: 200}}, {{state: 'off', duration: 1500}},
            ];
            let sosIndex = 0;
            function runSosStep() {{
                if (currentModeFromServer !== 'sos') return;
                const step = sosPattern[sosIndex]; updateVisuals(step.state, 'sos', -1, {{}}, 0, {{}}, {{}}, 0, {{}}, {{}}, {{}});
                sosIndex = (sosIndex + 1) % sosPattern.length;
                localAnimationId = setTimeout(runSosStep, step.duration);
            }}
            runSosStep();
        }}
        function handleLightClick(color) {{ stopLocalAnimation(); fetch(`/?action=set_color&color=${{color}}`); }}
        let lastModeClickTime = 0;
        function handleModeClick(mode) {{
            const now = Date.now();
            if (now - lastModeClickTime < 400) return;
            lastModeClickTime = now;
            const isTogglingOff = currentModeFromServer === mode;
            stopLocalAnimation(); fetch(`/?action=set_mode&mode=${{mode}}`);
            if (!isTogglingOff) {{ if (mode === 'party') startPartyAnimation(); else if (mode === 'sos') startSosAnimation(); }}
        }}
        function setConnected(ok) {{
            const pill = document.getElementById('connPill');
            const label = document.getElementById('connLabel');
            pill.className = ok ? 'conn-pill live' : 'conn-pill lost';
            label.textContent = ok ? 'Live' : 'Reconnecting…';
        }}
        function applyStatus(status) {{
            if (localAnimationId) return;
            updateVisuals(status.color, status.mode, status.s_bahn_minutes, status.weather, status.race_step, status.space_weather, status.traffic, status.audio_level, status.s_bahn_transit, status.environment, status.gremio, status.iracing_rev_pct);
        }}
        let eventSource = null;
        function connectEvents() {{
            eventSource = new EventSource('/events');
            eventSource.onmessage = (evt) => {{
                try {{ applyStatus(JSON.parse(evt.data)); }} catch (e) {{}}
                setConnected(true);
            }};
            eventSource.onopen = () => setConnected(true);
            eventSource.onerror = () => setConnected(false);
        }}
        connectEvents();
    </script>
    </body></html>
    """

class _Handler(BaseHTTPRequestHandler):
    controller: 'TrafficLightController' = None
    def log_message(self, fmt: str, *args) -> None:
        logging.debug(f"{self.address_string()} - {fmt % args}")
    def do_GET(self):
        p = urlparse(self.path)
        if p.path == '/status': self._status()
        elif p.path == '/events': self._events()
        elif p.path == '/': self._index(p)
        else: self._err(404,'Not found')
    def _status(self):
        try:
            data = self.controller.get_status(); self._json(data)
        except Exception as e:
            logging.error(f"status error: {e}"); self._err(500,'error')
    def _events(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        last_payload = None
        try:
            while self.controller.state.running:
                payload = json.dumps(self.controller.get_status())
                if payload != last_payload:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    last_payload = payload
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def _index(self,p):
        q = parse_qs(p.query); act = q.get('action',[None])[0]
        if act:
            try:
                if act=='set_color':
                    color = q.get('color',[''])[0];
                    if color: self.controller.set_manual_color(color)
                elif act=='set_mode':
                    mode = q.get('mode',[''])[0];
                    if mode: self.controller.set_mode(mode)
                self.send_response(200); self.end_headers(); return
            except Exception as e:
                logging.error(f"action error {act}: {e}"); self._err(400,'bad'); return
        self._html(_HTML)
    def _json(self, data: Dict):
        self.send_response(200); self.send_header('Content-type','application/json'); self.send_header('Cache-Control','no-cache'); self.end_headers(); self.wfile.write(json.dumps(data).encode())
    def _html(self, html: str):
        self.send_response(200); self.send_header('Content-type','text/html'); self.send_header('Cache-Control','no-store, no-cache, must-revalidate'); self.send_header('Pragma','no-cache'); self.send_header('Expires','0'); self.end_headers(); self.wfile.write(html.encode())
    def _err(self, code: int, msg: str):
        self.send_response(code); self.send_header('Content-type','text/plain'); self.end_headers(); self.wfile.write(msg.encode())

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# --------------------------- Controller ------------------------------------
class TrafficLightController:
    def __init__(self, use_mock_hardware: bool = False):
        self.state = SharedState()
        self.hardware = self._init_hardware(use_mock_hardware)
        self.mode_handlers = {
            'auto': handle_auto_mode,
            'party': handle_party_mode,
            'emergency': handle_emergency_mode,
            'sos': handle_sos_mode,
            's_bahn': handle_s_bahn_mode,
            'biergarten': handle_biergarten_mode,
            'racing': handle_racing_mode,
            'space': handle_space_mode,
            'stau': handle_stau_mode,
            'audio_vu': handle_audio_vu_mode,
            'uv': handle_uv_mode,
            'gremio': handle_gremio_mode,
        }
    def _init_hardware(self, use_mock_hardware: bool) -> LightHardware:
        if use_mock_hardware:
            return LightHardware(use_mock=True)
        # gpiozero/lgpio has a quirk where a *failed* pin claim (e.g. the previous
        # process's GPIO lines not released yet) can leave that pin's internal
        # registry entry permanently stuck for the rest of this process's life —
        # retrying the construction again never clears it. So the real fix is to
        # avoid ever failing the first attempt: give the kernel a moment up front.
        sleep(1.0)
        last_error: Optional[Exception] = None
        attempts = 15
        for attempt in range(attempts):
            try:
                hardware = LightHardware(use_mock=False)
                logging.info("Hardware initialized (mock=False)")
                return hardware
            except Exception as e:
                last_error = e
                logging.warning(f"Hardware init attempt {attempt + 1}/{attempts} failed: {e}; retrying")
                try:
                    # A failed construction can leave gpiozero's own internal pin
                    # registry holding a stale entry we never got a reference to
                    # (and so can't .close() ourselves); reset it before retrying.
                    from gpiozero import Device
                    Device.close_all()
                except Exception:
                    pass
                sleep(0.5)
        logging.error(f"Hardware init failed after retries: {last_error}; using mock")
        return LightHardware(use_mock=True)
    def set_light_state(self, color: str) -> None:
        with self.state.lock:
            if self.state.current_color == color: return
            prev = self.state.current_color
            try:
                self.hardware.set_state(color); self.state.current_color = color
                if self.state.current_mode == 'audio_vu':
                    now = time()
                    self.state.mode_state['audio_vu_last_step_time'] = now
                    self.state.mode_state['audio_vu_ignore_until'] = now + AUDIO_VU_RELAY_IGNORE_SECONDS
                logging.info(f"Light: {prev} -> {color}")
            except Exception as e:
                logging.error(f"Failed to set light {color}: {e}")
    def set_mode(self, mode: str) -> None:
        with self.state.lock:
            if self.state.current_mode == mode:
                self.state.target_mode = 'idle'; logging.info(f"Toggle off mode {mode}")
            else:
                self.state.target_mode = mode; logging.info(f"Switch to mode {mode}")
    def set_manual_color(self, color: str) -> None:
        with self.state.lock:
            if self.state.current_mode == 'manual' and self.state.current_color == color:
                self.state.target_manual_color = 'off'
            else:
                self.state.target_manual_color = color
            self.state.target_mode = 'manual'; logging.info(f"Manual color {color}")
    def get_status(self) -> Dict:
        with self.state.lock: return self.state.snapshot()
    def run_initialization_sequence(self):
        logging.info("Init sequence...")
        self.hardware.test_sequence(); logging.info("Init sequence done")
    def run(self):
        logging.info("Controller loop start")
        with self.state.lock:
            # Initialize to the requested target mode to avoid a brief startup flash.
            if self.state.target_mode == 'manual':
                self.state.current_mode = 'manual'
                self.set_light_state(self.state.target_manual_color)
            elif self.state.target_mode == 'idle':
                self.state.current_mode = 'idle'
                self.set_light_state('off')
            else:
                # Default behavior: start green and let the controller loop/handlers take over.
                self.set_light_state('green')
            self.state.last_state_change_time = time()
        while self.state.running:
            slp = CONTROLLER_LOOP_SLEEP
            with self.state.lock:
                if self.state.current_mode != self.state.target_mode:
                    self._transition_to_mode(self.state.target_mode)
                if self.state.current_mode == 'manual':
                    self.set_light_state(self.state.target_manual_color)
                elapsed = time() - self.state.last_state_change_time
                handler = self.mode_handlers.get(self.state.current_mode)
            # Run handler without holding the big lock so HTTP /status
            # requests aren't blocked by per-tick work. set_light_state and
            # other state writers acquire the lock themselves as needed.
            if handler:
                cs = handler(self, elapsed)
                if cs is not None:
                    slp = cs
            sleep(slp)
        logging.info("Controller loop stopped")
    def _transition_to_mode(self, new_mode: str):
        logging.info(f"Transition {self.state.current_mode} -> {new_mode}")
        self.state.current_mode = new_mode; self.state.last_state_change_time = time()
        if new_mode == 'auto':
            # Start in green so the first visible cycle isn't a 20s red hold.
            self.set_light_state('green'); self.state.mode_state['next_auto_state'] = 'red'
        elif new_mode == 'sos':
            self.state.mode_state['sos_index'] = 0
            # Apply the first pattern step on entry; handler advances after its duration.
            self.set_light_state(self.state.mode_state['sos_pattern'][0]['state'])
        elif new_mode == 'racing':
            self.state.mode_state['race_step'] = 0; self.set_light_state('off')
        elif new_mode == 'audio_vu':
            self.set_light_state('off'); self.state.audio_vu_level = 0.0; self.state.mode_state['audio_vu_last'] = time(); self.state.mode_state['audio_vu_above_since'] = None; self.state.mode_state['audio_vu_below_since'] = None; self.state.mode_state['audio_vu_ignore_until'] = time() + AUDIO_VU_RELAY_IGNORE_SECONDS; self.state.mode_state['audio_vu_last_step_time'] = time(); self.state.mode_state['audio_vu_baseline'] = 0.0; self.state.mode_state['audio_vu_last_beat'] = 0.0; self.state.mode_state['audio_vu_beat_hold_until'] = 0.0
        elif new_mode == 'idle':
            self.set_light_state('off')
    def shutdown(self):
        logging.info("Shutdown initiated")
        with self.state.lock: self.state.running = False
        self.hardware.all_off(); self.hardware.cleanup(); logging.info("Shutdown complete")

# --------------------------- Main ------------------------------------------

def _start_web(controller: TrafficLightController):
    _Handler.controller = controller
    srv = _ThreadingHTTPServer((WEB_SERVER_HOST, WEB_SERVER_PORT), _Handler)
    logging.info(f"Web server http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown(); logging.info("Web server stopped")

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.info("Starting single-file traffic light")
    ctrl = TrafficLightController(use_mock_hardware=False)

    # Prime network state once before threads start so the first tick has
    # a correct target mode; the network_monitor thread keeps it in sync.
    try:
        connected = _is_network_connected()
        with ctrl.state.lock:
            ctrl.state.network_connected = connected
            if connected:
                ctrl.state.target_mode = 'manual'
                ctrl.state.target_manual_color = 'off'
            else:
                ctrl.state.target_mode = 'auto'
    except Exception:
        pass

    try:
        ctrl.run_initialization_sequence()
    except Exception as e:
        logging.error(f"Init sequence error: {e}")
    def _sig(sig, frame):
        logging.info(f"Signal {sig} received")
        ctrl.shutdown(); sys.exit(0)
    signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)
    threads = [
        threading.Thread(target=ctrl.run, name='controller', daemon=True),
        threading.Thread(target=s_bahn_monitor, args=(ctrl,), name='s_bahn', daemon=True),
        threading.Thread(target=weather_monitor, args=(ctrl,), name='weather', daemon=True),
        threading.Thread(target=space_weather_monitor, args=(ctrl,), name='space', daemon=True),
        threading.Thread(target=traffic_monitor, args=(ctrl,), name='traffic', daemon=True),
        threading.Thread(target=iracing_udp_listener, args=(ctrl,), name='iracing', daemon=True),
        threading.Thread(target=audio_vu_monitor, args=(ctrl,), name='audio_vu', daemon=True),
        threading.Thread(target=network_monitor, args=(ctrl,), name='network', daemon=True),
        threading.Thread(target=transit_monitor, args=(ctrl,), name='transit', daemon=True),
        threading.Thread(target=environment_monitor, args=(ctrl,), name='environment', daemon=True),
        threading.Thread(target=gremio_monitor, args=(ctrl,), name='gremio', daemon=True),
        threading.Thread(target=_start_web, args=(ctrl,), name='web', daemon=True),  # web blocks; daemon so shutdown doesn't hang
    ]
    for t in threads[:-1]: t.start(); logging.info(f"Started thread {t.name}")
    threads[-1].start()  # start web last (blocking)
    threads[-1].join()

if __name__ == '__main__':
    main()
