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
API_TIMEOUT = 15.0

# Location / routes
WEATHER_LAT = "48.0667"
WEATHER_LON = "11.7167"
S_BAHN_EVA = "8004733"  # Ottobrunn
OUTBOUND_DESTINATIONS: Tuple[str, ...] = (
    "Kreuzstraße","Aying","Höhenkirchen-Siegertsbrunn","Dürrnhaar","Hohenbrunn","Wächterhof"
)
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

# API URLs
DB_API_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/plan"
WEATHER_API_URL_TEMPLATE = "https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric"
SPACE_WEATHER_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
GOOGLE_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"

# --------------------------- Shared State ----------------------------------
@dataclass
class SharedState:
    target_mode: str = "auto"
    target_manual_color: str = "off"
    current_mode: str = "auto"
    current_color: str = "unknown"
    last_state_change_time: float = field(default_factory=time)
    s_bahn_minutes_away: int = -1
    weather_status: Dict = field(default_factory=dict)
    iracing_light_status: str = "black"
    space_weather_status: Dict = field(default_factory=dict)
    traffic_status: Dict = field(default_factory=dict)
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
        ]
    })
    lock: threading.RLock = field(default_factory=threading.RLock)
    running: bool = True

    def snapshot(self) -> Dict:
        return {
            'color': self.current_color,
            'mode': self.current_mode,
            's_bahn_minutes': self.s_bahn_minutes_away,
            'weather': self.weather_status.copy(),
            'race_step': self.mode_state.get('race_step', 0),
            'space_weather': self.space_weather_status.copy(),
            'traffic': self.traffic_status.copy(),
        }

# --------------------------- Hardware --------------------------------------
class LEDInterface(Protocol):
    def on(self) -> None: ...
    def off(self) -> None: ...

class HardwareLED:
    def __init__(self, pin: int, active_high: bool = False):
        from gpiozero import LED  # Lazy import
        self._led = LED(pin, active_high=active_high)
    def on(self) -> None: self._led.on()
    def off(self) -> None: self._led.off()

class MockLED:
    def __init__(self, pin: int, active_high: bool = False):
        self.pin = pin; self.active_high = active_high; self.is_on = False
    def on(self) -> None: self.is_on = True
    def off(self) -> None: self.is_on = False

class LightHardware:
    def __init__(self, use_mock: bool = False):
        led_cls = MockLED if use_mock else HardwareLED
        self.red = led_cls(RED_PIN, ACTIVE_HIGH)
        self.yellow = led_cls(YELLOW_PIN, ACTIVE_HIGH)
        self.green = led_cls(GREEN_PIN, ACTIVE_HIGH)
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

# --------------------------- Monitors --------------------------------------
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
        with controller.state.lock:
            controller.state.weather_status = _fetch_weather()
        sleep(WEATHER_POLL_INTERVAL)

# Space Weather

def _fetch_space() -> Dict:
    try:
        r = requests.get(SPACE_WEATHER_URL, timeout=API_TIMEOUT); r.raise_for_status(); data = r.json(); latest = data[-1]
        kp = int(float(latest[1])); cond = 'Storm' if kp >= 5 else ('Active' if kp == 4 else 'Quiet')
        return {'kp_index': kp, 'condition': cond}
    except Exception: return {}

def space_weather_monitor(controller):
    while controller.state.running:
        with controller.state.lock:
            controller.state.space_weather_status = _fetch_space()
        sleep(SPACE_POLL_INTERVAL)

# Traffic

def _fetch_traffic() -> Dict:
    if not GOOGLE_MAPS_API_KEY: return {}
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

def traffic_monitor(controller):
    if not GOOGLE_MAPS_API_KEY: logging.warning("Traffic disabled: GOOGLE_MAPS_API_KEY missing"); return
    while controller.state.running:
        with controller.state.lock:
            controller.state.traffic_status = _fetch_traffic()
        sleep(TRAFFIC_POLL_INTERVAL)

# iRacing UDP

def iracing_udp_listener(controller):
    valid = {'red','yellow','green','black','green-yellow','all_on'}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((IRACING_UDP_HOST, IRACING_UDP_PORT)); sock.settimeout(1.0)
            while controller.state.running:
                try:
                    data, _ = sock.recvfrom(1024); color = data.decode('utf-8').strip()
                    if color in valid:
                        with controller.state.lock: controller.state.iracing_light_status = color
                except socket.timeout: continue
                except Exception: continue
    except Exception as e:
        logging.error(f"iRacing listener error: {e}")

# --------------------------- Web Server ------------------------------------
_HTML = f"""
    <!DOCTYPE html><html lang="en"><head><title>Traffic Light Control</title><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>:root{{--bg-color:#1a1d23;--body-bg:#111317;--text-color:#e0e0e0;--text-muted:#888;--accent-color:#007bff;--shadow-color:rgba(0,0,0,0.5)}}html,body{{height:100%;margin:0;padding:0;background-color:var(--body-bg);font-family:'Inter',sans-serif;color:var(--text-color);-webkit-tap-highlight-color:transparent;display:flex;justify-content:center;align-items:center}}.container{{width:100%;max-width:380px;padding:20px;box-sizing:border-box;display:flex;flex-direction:column;align-items:center;gap:25px}}.traffic-light-body{{background-color:var(--bg-color);border-radius:24px;padding:20px;display:flex;flex-direction:column;gap:15px;border:1px solid #333;box-shadow:0 10px 30px var(--shadow-color)}}.light{{width:90px;height:90px;border-radius:50%;background-color:#333;opacity:0.5;transition:all .15s ease-in-out;cursor:pointer;box-shadow:inset 0 2px 10px rgba(0,0,0,.4)}}.red-on{{background-color:#ff1c1c;opacity:1;box-shadow:0 0 40px #ff1c1c,inset 0 2px 10px rgba(0,0,0,.4)}}.yellow-on{{background-color:#ffc700;opacity:1;box-shadow:0 0 40px #ffc700,inset 0 2px 10px rgba(0,0,0,.4)}}.green-on{{background-color:#00ff00;opacity:1;box-shadow:0 0 40px #00ff00,inset 0 2px 10px rgba(0,0,0,.4)}}.controls{{text-align:center;width:100%}}#modeText{{font-size:1.5em;font-weight:600;margin-top:0;margin-bottom:8px}}.info-text{{height:22px;font-size:1em;font-style:italic;color:var(--text-muted);margin-bottom:20px}}.mode-buttons{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;width:100%}}.mode-buttons a{{background-color:#333;color:var(--text-color);padding:12px 10px;border-radius:12px;font-size:1em;font-weight:600;text-decoration:none;transition:background-color .2s,transform .1s}}.mode-buttons a:active{{transform:scale(.95)}}.mode-buttons a.active{{background-color:var(--accent-color);color:#fff}}</style></head>
    <body><div class="container"><div class="traffic-light-body" id="traffic-light"><div id="red" class="light" onclick="handleLightClick('red')"></div><div id="yellow" class="light" onclick="handleLightClick('yellow')"></div><div id="green" class="light" onclick="handleLightClick('green')"></div></div><div class="controls"><h2 id="modeText">Current Mode: <strong></strong></h2><div id="info-display" class="info-text"></div><div class="mode-buttons"><a href="#" id="mode-auto" onclick="handleModeClick('auto')">Auto</a><a href="#" id="mode-emergency" onclick="handleModeClick('emergency')">Emergency</a><a href="#" id="mode-sos" onclick="handleModeClick('sos')">SOS</a><a href="#" id="mode-party" onclick="handleModeClick('party')">Party</a><a href="#" id="mode-s_bahn" onclick="handleModeClick('s_bahn')">S-Bahn</a><a href="#" id="mode-biergarten" onclick="handleModeClick('biergarten')">Biergarten</a><a href="#" id="mode-racing" onclick="handleModeClick('racing')">Racing</a><a href="#" id="mode-stau" onclick="handleModeClick('stau')">Stau</a><a href="#" id="mode-space" onclick="handleModeClick('space')">Space</a></div></div></div>
    <script>
        let currentModeFromServer = 'unknown'; let localAnimationId = null;
        function updateVisuals(color, mode, s_bahn_minutes, weather, race_step, space_weather, traffic) {{
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
            if (mode === 's_bahn') {{ infoDisplay.textContent = (s_bahn_minutes === -1) ? 'No S-Bahn data.' : `Next train in ${{s_bahn_minutes}} min.`; }}
            else if (mode === 'biergarten') {{
                if (weather && weather.temp && weather.condition) {{ infoDisplay.textContent = `${{weather.temp.toFixed(1)}}°C, ${{weather.condition}}`; }}
                else {{ infoDisplay.textContent = 'No weather data.'; }}
            }}
            else if (mode === 'racing' && race_step >= 4) {{ infoDisplay.textContent = 'Listening for iRacing...'; }}
            else if (mode === 'space') {{
                if (space_weather && space_weather.kp_index !== undefined) {{ infoDisplay.textContent = `Kp-index: ${{space_weather.kp_index}} (${{space_weather.condition}})`; }}
                else {{ infoDisplay.textContent = 'No space weather data.'; }}
            }}
            else if (mode === 'stau') {{
                if (traffic && traffic.commute_time) {{ infoDisplay.textContent = `Commute: ${{traffic.commute_time}}`; }}
                else {{ infoDisplay.textContent = 'No traffic data.'; }}
            }}
            else {{ infoDisplay.textContent = ''; }}
            const isRedOn = color === 'red' || color === 'red_and_yellow' || color === 'all_on';
            const isYellowOn = color === 'yellow' || color === 'red_and_yellow' || color === 'all_on' || color === 'green-yellow';
            const isGreenOn = color === 'green' || color === 'all_on' || color === 'green-yellow';
            document.getElementById('red').className = 'light' + (isRedOn ? ' red-on' : '');
            document.getElementById('yellow').className = 'light' + (isYellowOn ? ' yellow-on' : '');
            document.getElementById('green').className = 'light' + (isGreenOn ? ' green-on' : '');
        }}
        function stopLocalAnimation() {{ if (localAnimationId) {{ clearInterval(localAnimationId); clearTimeout(localAnimationId); localAnimationId = null; }} }}
        function startPartyAnimation() {{ stopLocalAnimation(); localAnimationId = setInterval(() => {{ const colors = ['red', 'yellow', 'green', 'off']; updateVisuals(colors[Math.floor(Math.random() * colors.length)], 'party', -1, {{}}, 0, {{}}, {{}}); }}, 80); }}
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
                const step = sosPattern[sosIndex]; updateVisuals(step.state, 'sos', -1, {{}}, 0, {{}}, {{}});
                sosIndex = (sosIndex + 1) % sosPattern.length;
                localAnimationId = setTimeout(runSosStep, step.duration);
            }}
            runSosStep();
        }}
        function handleLightClick(color) {{ stopLocalAnimation(); fetch(`/?action=set_color&color=${{color}}`); }}
        function handleModeClick(mode) {{
            const isTogglingOff = currentModeFromServer === mode;
            stopLocalAnimation(); fetch(`/?action=set_mode&mode=${{mode}}`);
            if (!isTogglingOff) {{ if (mode === 'party') startPartyAnimation(); else if (mode === 'sos') startSosAnimation(); }}
        }}
        async function syncWithServer() {{
            if (localAnimationId) return;
            try {{
                const response = await fetch('/status');
                const status = await response.json();
                updateVisuals(status.color, status.mode, status.s_bahn_minutes, status.weather, status.race_step, status.space_weather, status.traffic);
            }} catch (e) {{}}
        }}
        setInterval(syncWithServer, 400);
        syncWithServer();
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
        elif p.path == '/': self._index(p)
        else: self._err(404,'Not found')
    def _status(self):
        try:
            data = self.controller.get_status(); self._json(data)
        except Exception as e:
            logging.error(f"status error: {e}"); self._err(500,'error')
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
        self.send_response(200); self.send_header('Content-type','application/json'); self.send_header('Cache-Control','no-cache'); self.end_headers(); self.wfile.write((__import__('json').dumps(data)).encode())
    def _html(self, html: str):
        self.send_response(200); self.send_header('Content-type','text/html'); self.send_header('Cache-Control','no-cache'); self.end_headers(); self.wfile.write(html.encode())
    def _err(self, code: int, msg: str):
        self.send_response(code); self.send_header('Content-type','text/plain'); self.end_headers(); self.wfile.write(msg.encode())

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# --------------------------- Controller ------------------------------------
class TrafficLightController:
    def __init__(self, use_mock_hardware: bool = False):
        self.state = SharedState()
        try:
            self.hardware = LightHardware(use_mock=use_mock_hardware)
            logging.info("Hardware initialized (mock=%s)", use_mock_hardware)
        except Exception as e:
            logging.error(f"Hardware init failed: {e}; using mock")
            self.hardware = LightHardware(use_mock=True)
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
        }
    def set_light_state(self, color: str) -> None:
        with self.state.lock:
            if self.state.current_color == color: return
            prev = self.state.current_color
            try:
                self.hardware.set_state(color); self.state.current_color = color
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
            self.set_light_state('green'); self.state.last_state_change_time = time()
        while self.state.running:
            slp = CONTROLLER_LOOP_SLEEP
            with self.state.lock:
                if self.state.current_mode != self.state.target_mode:
                    self._transition_to_mode(self.state.target_mode)
                if self.state.current_mode == 'manual':
                    self.set_light_state(self.state.target_manual_color)
                elapsed = time() - self.state.last_state_change_time
                handler = self.mode_handlers.get(self.state.current_mode)
                if handler:
                    cs = handler(self, elapsed)
                    if cs is not None: slp = cs
            sleep(slp)
        logging.info("Controller loop stopped")
    def _transition_to_mode(self, new_mode: str):
        logging.info(f"Transition {self.state.current_mode} -> {new_mode}")
        self.state.current_mode = new_mode; self.state.last_state_change_time = time()
        if new_mode == 'auto':
            self.set_light_state('red'); self.state.mode_state['next_auto_state'] = 'red_and_yellow'
        elif new_mode == 'sos':
            self.state.mode_state['sos_index'] = 0; self.set_light_state('off')
        elif new_mode == 'racing':
            self.state.mode_state['race_step'] = 0; self.set_light_state('off')
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
        threading.Thread(target=_start_web, args=(ctrl,), name='web', daemon=False),  # web blocks
    ]
    for t in threads[:-1]: t.start(); logging.info(f"Started thread {t.name}")
    threads[-1].start()  # start web last (blocking)
    threads[-1].join()

if __name__ == '__main__':
    main()
