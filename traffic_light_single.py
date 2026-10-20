#!/usr/bin/env python3
"""Single-file Traffic Light Control System (fully consolidated).

Includes: configuration, shared state, hardware abstraction (real/mock),
mode handlers, monitors (S-Bahn, weather, space, traffic, SimHub UDP),
web server, controller class, and main entry point.

Run: python3 traffic_light/traffic_light_single.py
Web UI: http://<pi-ip>:8000
"""
from __future__ import annotations
import os
import sys
import signal
import logging
import math
import threading
from collections import deque
from time import time, sleep
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, List, Tuple
from datetime import datetime, timedelta
import random
import shlex
import shutil
import subprocess
import tempfile
import socket
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
SIMHUB_UDP_HOST = "0.0.0.0"
SIMHUB_UDP_PORT = 9001

# Timing (seconds)
AUTO_GREEN_DURATION = 20.0
AUTO_YELLOW_DURATION = 3.0
AUTO_RED_DURATION = 20.0
AUTO_RED_YELLOW_DURATION = 2.0
EMERGENCY_BLINK_INTERVAL = 0.5
CONTROLLER_LOOP_SLEEP = 0.2
SSE_KEEPALIVE_SECONDS = 15.0
# Intro played when a Racing sub-mode is selected: (lamps, seconds) steps.
# One rev to the limiter: quick climb, hold on redline, drop back, short breath.
_REV_BLIP = [('green', 0.1), ('green-yellow', 0.1), ('all_on', 0.6), ('green-yellow', 0.12), ('green', 0.12), ('off', 0.35)]
RACING_INTROS = {
    'race_start': [('off', 1.0), ('red', 1.0), ('red_and_yellow', 1.0), ('all_on', 1.0), ('off', 0.5)],  # start lights
    'racing': _REV_BLIP * 2,                                                                            # two revs to the limiter
    'hotlap': [('green', 0.4), ('yellow', 0.4), ('green', 0.4), ('yellow', 0.4), ('red', 0.8), ('off', 0.4)],  # delta battle, then invalid
}
# Hotlap (Racing sub-mode): live delta to your best lap from the SimHub plugin.
HOTLAP_DEADBAND = 0.03       # +-s around 0 where the colour holds, so being exactly on pace doesn't chatter the relays
HOTLAP_MIN_SWITCH = 0.25     # s between green/yellow changes (red always switches at once)
SIMHUB_STALE_SECONDS = 2.0   # no packet from the plugin for this long (game closed) -> Racing lights off
S_BAHN_POLL_INTERVAL = 30.0
S_BAHN_STALE_SECONDS = 300.0   # keep counting down from the last timetable this long when fetches fail
WEATHER_POLL_INTERVAL = 900.0
SPACE_POLL_INTERVAL = 900.0
TRAFFIC_POLL_INTERVAL = 600.0
NETWORK_POLL_INTERVAL = 10.0
LAST_MODE_FILE = os.path.expanduser(os.getenv("LAST_MODE_FILE", "~/.traffic_light_mode.json"))
TRANSIT_POLL_INTERVAL = 600.0
ENVIRONMENT_POLL_INTERVAL = 600.0   # also feeds Temperature and Rain, so keep it fresh
GREMIO_POLL_INTERVAL = 3600.0
API_TIMEOUT = 15.0
POLL_RETRY_SECONDS = 30.0      # first retry after a failed fetch; doubles up to the poll interval
POLL_STALE_INTERVALS = 3       # keep the last good data this many poll intervals before showing "no data"
AUDIO_INPUT_DEVICE = os.getenv("AUDIO_INPUT_DEVICE", "").strip()
AUDIO_RETRY_SECONDS = 5.0       # mic missing or failed: look again after this, doubling...
AUDIO_RETRY_MAX_SECONDS = 60.0  # ...up to this

# Party mode - beat-synced light show driven by the USB mic.
# Pipeline (all numpy, runs in the audio thread only while Party is active):
#   1. Onset envelope: spectral flux (sum of positive frame-to-frame rises in
#      log-magnitude) per 1024-sample block, in two bands. The bass band
#      (kick drum) carries the beat; the mid band helps on kick-less music.
#   2. Tempo: autocorrelation of the last few seconds of that envelope,
#      weighted toward ~120 BPM so it prefers the beat over 8th notes / half
#      time (same idea as librosa's tempo estimator, done incrementally).
#   3. Phase: comb-filter the envelope at the found period to locate where
#      the beats land, then predict the next ones so the relays switch *on*
#      the beat instead of a block late.
#   4. Lights: one random lamp on every predicted beat (a different one
#      each time), so the light hops in time with the music.
# The relays click when they switch and the mic hears it. The click is
# mostly above ~1 kHz, so while it rings only the mid band is masked - the
# bass band keeps running, otherwise every beat we switch on would be erased
# from the envelope and the tracker would lose its own lock.
PARTY_TEMPO_WINDOW_SECONDS = 8.0   # onset history used for tempo/phase
PARTY_MIN_HISTORY_SECONDS = 3.0    # before this, no tempo guess at all
PARTY_TEMPO_UPDATE_SECONDS = 0.5
PARTY_MIN_BPM = 60.0
PARTY_MAX_BPM = 180.0
PARTY_PRIOR_BPM = 120.0            # centre of the tempo preference
PARTY_PRIOR_OCTAVES = 1.0          # width of that preference (log2 std dev)
PARTY_LOCK_CONFIDENCE = 0.20       # normalized autocorrelation needed to lock
PARTY_UNLOCK_UPDATES = 4           # this many weak estimates in a row -> unlock
PARTY_TEMPO_CHANGE_UPDATES = 3     # a new tempo must persist this long to win
PARTY_BASS_BAND = (40.0, 200.0)
PARTY_MID_BAND = (200.0, 5000.0)
PARTY_MID_WEIGHT = 0.5
PARTY_CLICK_MASK_SECONDS = 0.20    # relay click rings ~120-200ms (measured)
PARTY_SILENCE_RMS = 0.02           # mic RMS below this = no music (mic gain 11/16: quiet room ~-45 dB, music ~-20 dB)
PARTY_CHILL_MAX_BPM = 95.0
PARTY_GROOVE_MAX_BPM = 125.0
PARTY_BEAT_LEAD_SECONDS = 0.03     # fire this early to cover relay + perception lag
PARTY_MIN_SWITCH_SECONDS = 0.33    # never switch the relays faster than this
# Music with no beat to lock onto (dense metal through a bass-heavy mic, rubato,
# ambient): react to individual hits instead. A hit is a block whose spectral
# flux stands PARTY_HIT_Z standard deviations above that band's last ~2 s.
PARTY_HIT_Z = 2.5
PARTY_HIT_TAU_SECONDS = 2.0        # how fast the per-band baseline adapts
PARTY_IDLE_DRIFT_SECONDS = 3.0     # no hits for this long: drift one step so it never freezes
PARTY_LOG_INTERVAL = 5.0           # tracker summary in the journal, for calibration
# /?action=party_record saves the last N seconds the tracker heard (relay
# clicks and all) to PARTY_RECORD_DIR, so live behaviour can be replayed offline.
PARTY_RECORD_SECONDS = 30.0
PARTY_RECORD_DIR = os.path.expanduser("~/party-cal")

# VU meter (Party sub-mode) - continuous mic-level meter.
# Level is normalized against a self-adjusting noise floor (an auto-ranging
# noise gate, not a fixed reference level), so it re-calibrates to whatever
# room/mic gain it's running in:
#   - floor tracks the recent quietest level: snaps down fast, creeps up slow.
#   - ceiling is derived from the floor by a fixed ratio, not tracked
#     independently (two adaptive envelopes chase each other and go unstable).
#   - displayed level = (raw - floor) / (floor*ratio - floor), clamped 0..1.
# Measured with Auto Gain Control off and mic gain 11/16: quiet room ~0.004-0.006 RMS.
AUDIO_VU_FLOOR_SEED = 0.005
AUDIO_VU_FLOOR_TAU_DOWN = 1.2   # seconds to mostly settle onto a new, quieter floor
AUDIO_VU_FLOOR_TAU_UP = 8.0     # seconds to drift the floor up if the room gets noisier
AUDIO_VU_CEILING_RATIO = 9.0    # "loud" = floor * this
AUDIO_VU_MIN_RANGE = 0.05       # floor..ceiling floor - guards near-zero floor
# Hysteresis bands on the normalized 0..1 scale: separate enter/exit thresholds
# per color so hovering near a boundary can't flap the relay back and forth.
AUDIO_VU_ENTER_GREEN = 0.16
AUDIO_VU_EXIT_GREEN = 0.08
AUDIO_VU_ENTER_YELLOW = 0.50
AUDIO_VU_EXIT_YELLOW = 0.36
AUDIO_VU_ENTER_ALL = 0.82
AUDIO_VU_EXIT_ALL = 0.66
AUDIO_VU_STALE_SECONDS = 0.12
AUDIO_VU_MODE_LOOP_SECONDS = 0.02
AUDIO_VU_SMOOTHING_ATTACK = 0.30
AUDIO_VU_SMOOTHING_RELEASE = 0.82
# Relay click decays to baseline within ~120-200ms (measured); mute the level
# tracker for longer than that so the click's own noise never reaches it.
AUDIO_VU_RELAY_IGNORE_SECONDS = 0.30
AUDIO_VU_MIN_STEP_SECONDS = 0.09

# Temperature (Weather sub-mode): green inside the comfortable band, yellow
# in the outer band, red beyond it.
TEMP_GREEN_RANGE = (15.0, 26.0)
TEMP_YELLOW_RANGE = (5.0, 30.0)
# Rain (Weather sub-mode), from the Open-Meteo hourly forecast: an hour counts
# as rainy at this much precipitation (mm) or this probability (%).
RAIN_MM = 0.2
RAIN_PROBABILITY = 60
RAIN_RED_MINUTES = 60              # rain this soon (or now) -> red
RAIN_YELLOW_MINUTES = 180          # rain within this -> yellow, else green

# Clock mode - quarter chimes like an old striking clock, as sweeps along the
# light (green bottom, red top). Lamps stay on as the sweep adds the next one,
# then all go dark together: up = green, +yellow, +red; down = red, +yellow,
# +green.
# :15 = up, :30 = up+down, :45 = up+down+up,
# :00 = up+down+up+down, then one yellow blink per hour (12h).
# Between chimes the lamps stay off, like a clock that's silent until it
# strikes (and it saves power).
CLOCK_BEAT = 0.75                  # time before the next lamp joins in a sweep
CLOCK_SWEEP_GAP = 0.75             # dark pause between sweeps
CLOCK_HOUR_GAP = 1.5               # pause between the last sweep and the first strike
CLOCK_STRIKE_ON = 0.75             # how long each hour strike stays lit
CLOCK_STRIKE_INTERVAL = 1.5        # strike to strike
CLOCK_QUIET_HOURS = (23, 7)        # no chimes from 23:00 to 06:59 (relays click)

# Westminster (Clock sub-mode) - the same clock, with the Westminster Quarters
# (Big Ben's tune, 1793) rung on synthesised bells through the speaker. Each
# sweep is one change of four notes: the lamps join on the first three and all
# go dark on the long fourth. :15 = 1 change, :30 = 2, :45 = 3,
# :00 = 4, then the hour bell strikes with each yellow blink.
WESTMINSTER_BELLS = (415.30, 369.99, 329.63, 246.94)   # G#4 F#4 E4 B3
WESTMINSTER_HOUR_BELL = 164.81                         # E3
WESTMINSTER_CHANGES = ((0, 1, 2, 3), (2, 0, 1, 3), (2, 1, 0, 2), (0, 2, 1, 3), (3, 1, 0, 2))
WESTMINSTER_QUARTERS = {1: (0,), 2: (1, 2), 3: (3, 4, 0), 4: (1, 2, 3, 4)}
WESTMINSTER_BEAT = 0.7             # note to note; the fourth note of a change rings two beats
WESTMINSTER_HOUR_GAP = 1.5         # after the last change, before the first hour strike
WESTMINSTER_STRIKE_INTERVAL = 2.2  # hour strike to hour strike
CHIME_VOLUME = float(os.getenv("CHIME_VOLUME", "0.5"))  # 0..1, on top of the system volume
CHIME_PLAYER = os.getenv("CHIME_PLAYER", "").strip()    # e.g. "aplay -D plughw:CARD=UC"; default: PipeWire/Pulse/ALSA, whichever is there
CHIME_LATENCY_SECONDS = 0.12       # the player starts this late; the bells are rung this much earlier

# Location / routes - everything that says where you live or work comes from
# the environment so it stays out of the repo.
WEATHER_LAT = os.getenv("WEATHER_LAT", "48.137").strip()   # default: Munich centre
WEATHER_LON = os.getenv("WEATHER_LON", "11.575").strip()
S_BAHN_EVA = os.getenv("S_BAHN_EVA", "").strip()  # your station's EVA number; S-Bahn mode is off without it
# Trains ending at these stations (comma-separated) go the wrong way and are ignored.
OUTBOUND_DESTINATIONS: Tuple[str, ...] = tuple(
    d.strip() for d in os.getenv("S_BAHN_SKIP_DESTINATIONS", "").split(",") if d.strip()
)
# Home -> work, for Stau and the public-transport commute; addresses are
# geocoded via TomTom and passed to MVG (Munich transit authority, free, no
# key). Without a home address there are no routes.
MVG_ROUTES_URL = "https://www.mvg.de/api/bgw-pt/v3/routes"
COMMUTE_ORIGIN = os.getenv("COMMUTE_ORIGIN", "").strip()
COMMUTE_DESTINATION = os.getenv("COMMUTE_DESTINATION", "").strip()
TRAFFIC_ROUTES: Tuple[dict, ...] = (
    *([{"name": "commute","origin": COMMUTE_ORIGIN,"destination": COMMUTE_DESTINATION}] if COMMUTE_ORIGIN and COMMUTE_DESTINATION else []),
    *([{"name": "center","origin": COMMUTE_ORIGIN,"destination": "Marienplatz, Munich, Germany"},
       {"name": "north","origin": COMMUTE_ORIGIN,"destination": "BMW Welt, Munich, Germany"}] if COMMUTE_ORIGIN else []),
)

# API keys (env)
DB_CLIENT_ID = os.getenv("DB_CLIENT_ID", "")
DB_CLIENT_SECRET = os.getenv("DB_CLIENT_SECRET", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "")

# API URLs
DB_API_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
SPACE_WEATHER_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
GOOGLE_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
TOMTOM_GEOCODE_URL = "https://api.tomtom.com/search/2/geocode/{query}.json"
TOMTOM_ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute/{loc}/json"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
GREMIO_ESPN_TEAM_ID = "6273"
# Every competition Grêmio might be playing in at once: domestic league + the CONMEBOL cups.
GREMIO_ESPN_LEAGUES = ["bra.1", "bra.copa_do_brazil", "conmebol.libertadores", "conmebol.sudamericana", "conmebol.recopa"]

def _gremio_schedule_url(league: str) -> str:
    return f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{GREMIO_ESPN_TEAM_ID}/schedule"

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
    simhub_light_status: str = "black"
    simhub_rev_pct: Optional[float] = None
    simhub_delta: Optional[float] = None
    simhub_flag_status: str = "black"
    simhub_lap_invalid: bool = False
    simhub_last_rx: float = 0.0
    space_weather_status: Dict = field(default_factory=dict)
    traffic_status: Dict = field(default_factory=dict)
    environment_status: Dict = field(default_factory=dict)
    gremio_status: Dict = field(default_factory=dict)
    party_status: Dict = field(default_factory=dict)
    audio_vu_level: float = 0.0
    network_connected: bool = False
    # What was last picked in the UI: restored when the network comes back and after a restart.
    chosen_mode: str = 'manual'
    chosen_color: str = 'off'
    mode_state: Dict = field(default_factory=lambda: {
        'next_auto_state': 'green',
        'sos_index': 0,
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
        'click_until': 0.0,
        'audio_vu_last': 0.0,
        'audio_vu_ignore_until': 0.0,
        'audio_vu_last_step_time': 0.0,
        'audio_vu_floor': AUDIO_VU_FLOOR_SEED,
        'audio_vu_last_update': 0.0,
    })
    lock: threading.RLock = field(default_factory=threading.RLock)
    running: bool = True

    def snapshot(self) -> Dict:
        return {
            'color': self.current_color,
            'mode': self.current_mode,
            's_bahn_minutes': self.s_bahn_minutes_away,
            's_bahn_transit': self.s_bahn_transit_status.copy(),
            'race_step': 0 if self.mode_state.get('intro_seq') else 4,  # the web UI waits for the intro to finish
            'simhub_rev_pct': self.simhub_rev_pct,
            'simhub_delta': self.simhub_delta,
            'simhub_lap_invalid': self.simhub_lap_invalid,
            'simhub_flag': self.simhub_flag_status,
            'simhub_live': time() - self.simhub_last_rx < SIMHUB_STALE_SECONDS,
            'space_weather': self.space_weather_status.copy(),
            'traffic': self.traffic_status.copy(),
            'environment': self.environment_status.copy(),
            'gremio': self.gremio_status.copy(),
            'party': self.party_status.copy(),
            'audio_level': self.audio_vu_level,
            'network_connected': self.network_connected,
        }

# --------------------------- Hardware --------------------------------------
LIGHT_STATES = ('off', 'red', 'yellow', 'green', 'red_and_yellow', 'green-yellow', 'all_on')

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
        # The pins belong to gpiozero, so it releases them (RPi.GPIO doesn't know them, and doesn't work on a Pi 5).
        for led in self.all_lights:
            try: led.off(); led.close()
            except Exception: pass

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
    w = controller.state.environment_status; temp = w.get('temp'); cond = w.get('condition'); hour = datetime.now().hour; c = controller.state.current_color
    if temp is None or cond is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif hour < 16 or temp < 15 or cond in ('Drizzle', 'Rain', 'Snow', 'Thunderstorm'): controller.set_light_state('red')
    elif temp < 18 or cond in ('Clouds', 'Fog'): controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def _play_intro(controller) -> Optional[float]:
    """Step through the sub-mode's intro; returns a sleep while it plays, None once it's done."""
    ms = controller.state.mode_state; seq = ms.get('intro_seq')
    if not seq:
        return None
    now = time()
    if now < ms.get('intro_until', 0.0):
        return 0.02
    idx = ms.get('intro_idx', -1) + 1
    if idx >= len(seq):
        ms['intro_seq'] = None
        return None
    color, dur = seq[idx]
    controller.set_light_state(color)
    ms['intro_idx'] = idx; ms['intro_until'] = now + dur
    return 0.02

def handle_racing_mode(controller, elapsed: float) -> Optional[float]:
    intro = _play_intro(controller)
    if intro is not None:
        return intro
    s = controller.state
    if time() - s.simhub_last_rx > SIMHUB_STALE_SECONDS:
        controller.set_light_state('off'); return 0.1  # the plugin stops sending when the game closes
    live = s.simhub_light_status if s.simhub_light_status != 'black' else 'off'; controller.set_light_state(live); return 0.05

def handle_race_start_mode(controller, elapsed: float) -> Optional[float]:
    # Race start lights and flags from the plugin: red = lights set, yellow = get ready / yellow flag, green = go.
    intro = _play_intro(controller)
    if intro is not None:
        return intro
    s = controller.state
    if time() - s.simhub_last_rx > SIMHUB_STALE_SECONDS:
        controller.set_light_state('off'); return 0.1
    flag = s.simhub_flag_status
    controller.set_light_state('off' if flag == 'black' else flag)
    return 0.02

def handle_hotlap_mode(controller, elapsed: float) -> Optional[float]:
    intro = _play_intro(controller)
    if intro is not None:
        return intro
    s = controller.state; now = time(); c = s.current_color
    if now - s.simhub_last_rx > SIMHUB_STALE_SECONDS:
        controller.set_light_state('off'); return 0.1
    if s.simhub_lap_invalid:
        controller.set_light_state('red'); return 0.02  # overrides the delta, immediately
    d = s.simhub_delta
    if d is None:
        controller.set_light_state('off'); return 0.05  # no reference lap yet
    if d < -HOTLAP_DEADBAND: want = 'green'
    elif d > HOTLAP_DEADBAND: want = 'yellow'
    else: want = c if c in ('green', 'yellow') else 'yellow'
    if want != c and c in ('green', 'yellow') and now - s.mode_state.get('hotlap_switch', 0.0) < HOTLAP_MIN_SWITCH:
        return 0.02
    if want != c:
        s.mode_state['hotlap_switch'] = now
    controller.set_light_state(want)
    return 0.02

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

PARTY_LAMPS = ('red', 'yellow', 'green')

def _party_hop(controller) -> None:
    # Light one lamp, never the one already on, so every switch is visible.
    current = controller.state.current_color
    controller.set_light_state(random.choice([c for c in PARTY_LAMPS if c != current]))

def handle_party_mode(controller, elapsed: float) -> Optional[float]:
    s = controller.state; ms = s.mode_state; now = time()
    with s.lock:
        p = dict(s.party_status)
    bpm = p.get('bpm'); anchor = p.get('beat_anchor'); vibe = p.get('vibe', 'silence')

    if vibe == 'silence':
        # Nothing playing: stay dark - flickering would just fill the quiet
        # room with relay clicks.
        controller.set_light_state('off')
        return 0.05
    if not bpm or anchor is None or vibe == 'ambient':
        # Music without a beat to lock onto: hop on each hit the tracker
        # hears, or drift now and then so it never freezes.
        hit = p.get('hit_seq', 0) != ms.get('party_seen_hit')
        if hit or now - ms.get('party_last_switch', 0.0) >= PARTY_IDLE_DRIFT_SECONDS:
            ms['party_seen_hit'] = p.get('hit_seq', 0)
            _party_hop(controller); ms['party_last_switch'] = now
        return 0.01

    # Fire once per predicted beat, a little early to cover relay latency.
    period = 60.0 / bpm
    t = now + PARTY_BEAT_LEAD_SECONDS
    beat = anchor + round((t - anchor) / period) * period
    if t < beat or beat <= ms.get('party_last_beat', 0.0) + 0.5 * period:
        return 0.01
    ms['party_last_beat'] = beat
    ms['party_beat_count'] = ms.get('party_beat_count', 0) + 1
    every = 1
    while every * period < PARTY_MIN_SWITCH_SECONDS:
        every *= 2  # very fast tracks: hop on every other beat instead
    if ms['party_beat_count'] % every == 0:
        _party_hop(controller); ms['party_last_switch'] = now
    return 0.01

def handle_audio_vu_mode(controller, elapsed: float) -> Optional[float]:
    state = controller.state
    now = time()

    ignore_until = float(state.mode_state.get('audio_vu_ignore_until', 0.0) or 0.0)
    if now < ignore_until:
        return AUDIO_VU_MODE_LOOP_SECONDS

    last = state.mode_state.get('audio_vu_last', 0.0)
    level = state.audio_vu_level if (now - last) <= AUDIO_VU_STALE_SECONDS else 0.0

    # Hysteresis (a Schmitt trigger): separate enter/exit thresholds per band,
    # keyed off the color we're currently sitting in, so a level hovering near
    # a boundary can't flap the relay back and forth every cycle.
    color = state.current_color
    if color == 'off':
        target_color = 'green' if level >= AUDIO_VU_ENTER_GREEN else 'off'
    elif color == 'green':
        if level < AUDIO_VU_EXIT_GREEN:
            target_color = 'off'
        elif level >= AUDIO_VU_ENTER_YELLOW:
            target_color = 'green-yellow'
        else:
            target_color = 'green'
    elif color == 'green-yellow':
        if level < AUDIO_VU_EXIT_YELLOW:
            target_color = 'green'
        elif level >= AUDIO_VU_ENTER_ALL:
            target_color = 'all_on'
        else:
            target_color = 'green-yellow'
    else:  # all_on
        target_color = 'all_on' if level >= AUDIO_VU_EXIT_ALL else 'green-yellow'

    if target_color != state.current_color:
        last_step = float(state.mode_state.get('audio_vu_last_step_time', 0.0) or 0.0)
        if (now - last_step) < AUDIO_VU_MIN_STEP_SECONDS:
            return AUDIO_VU_MODE_LOOP_SECONDS

    controller.set_light_state(target_color)
    return AUDIO_VU_MODE_LOOP_SECONDS

def handle_temp_mode(controller, elapsed: float) -> Optional[float]:
    temp = controller.state.environment_status.get('temp'); c = controller.state.current_color
    if temp is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    if TEMP_GREEN_RANGE[0] <= temp <= TEMP_GREEN_RANGE[1]: controller.set_light_state('green')
    elif TEMP_YELLOW_RANGE[0] <= temp <= TEMP_YELLOW_RANGE[1]: controller.set_light_state('yellow')
    else: controller.set_light_state('red')

def handle_rain_mode(controller, elapsed: float) -> Optional[float]:
    env = controller.state.environment_status; c = controller.state.current_color
    if 'rain_now' not in env: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    eta = env.get('rain_eta_min')
    if env['rain_now'] or (eta is not None and eta <= RAIN_RED_MINUTES): controller.set_light_state('red')
    elif eta is not None and eta <= RAIN_YELLOW_MINUTES: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def _clock_quiet(hour: int) -> bool:
    start, end = CLOCK_QUIET_HOURS
    return (hour >= start or hour < end) if start > end else (start <= hour < end)

def clock_chime_sequence(quarter: int, hour12: int) -> List[Tuple[str, float]]:
    """Steps for one chime. quarter 1..3 = :15/:30/:45, 4 = full hour (+ hour strikes)."""
    seq: List[Tuple[str, float]] = [('off', 1.0)]  # a beat of darkness before the chime
    up = ['green', 'green-yellow', 'all_on']
    down = ['red', 'red_and_yellow', 'all_on']
    for i in range(quarter):
        if i:
            seq.append(('off', CLOCK_SWEEP_GAP))
        for color in (up if i % 2 == 0 else down):
            seq.append((color, CLOCK_BEAT))
    seq.append(('off', CLOCK_HOUR_GAP if quarter == 4 else 1.0))
    if quarter == 4:
        for i in range(hour12):
            seq.append(('yellow', CLOCK_STRIKE_ON))
            seq.append(('off', CLOCK_STRIKE_INTERVAL - CLOCK_STRIKE_ON if i + 1 < hour12 else 1.0))
    return seq

def westminster_sequence(quarter: int, hour12: int) -> Tuple[List[Tuple[str, float]], List[Tuple[float, float, float]]]:
    """Light steps and bell notes (start s, Hz, strength) for one chime; quarter 4 = full hour."""
    b = WESTMINSTER_BEAT; t = 1.0
    steps: List[Tuple[str, float]] = [('off', t)]
    notes: List[Tuple[float, float, float]] = []
    up = ['green', 'green-yellow', 'all_on']
    down = ['red', 'red_and_yellow', 'all_on']
    for i, change in enumerate(WESTMINSTER_QUARTERS[quarter]):
        lamps = up if i % 2 == 0 else down
        for k, bell in enumerate(WESTMINSTER_CHANGES[change]):
            notes.append((t + k * b, WESTMINSTER_BELLS[bell], 1.0 if k < 3 else 1.15))
        steps += [(lamps[0], b), (lamps[1], b), (lamps[2], b), ('off', 2 * b)]  # the fourth note puts them out
        t += 5 * b
    if quarter == 4:
        steps.append(('off', WESTMINSTER_HOUR_GAP)); t += WESTMINSTER_HOUR_GAP
        for _ in range(hour12):
            notes.append((t, WESTMINSTER_HOUR_BELL, 1.4))
            steps += [('yellow', CLOCK_STRIKE_ON), ('off', WESTMINSTER_STRIKE_INTERVAL - CLOCK_STRIKE_ON)]
            t += WESTMINSTER_STRIKE_INTERVAL
    steps.append(('off', 1.0))
    return steps, notes

# Church-bell partials: (ratio to the strike note, loudness, decay seconds) - hum,
# prime, minor-third tierce, quint, nominal and a few upper ones.
_BELL_PARTIALS = ((0.5, 0.25, 3.5), (1.0, 0.55, 2.4), (1.2, 0.30, 1.8), (1.5, 0.18, 1.4),
                  (2.0, 0.42, 1.1), (2.51, 0.14, 0.7), (3.0, 0.12, 0.5), (4.07, 0.07, 0.3))

_bell_tones: Dict[float, object] = {}  # one synthesised strike per pitch, reused

def _bell_tone(np, freq: float, rate: int):
    if freq not in _bell_tones:
        tt = np.arange(int(5.0 * rate)) / rate  # each bell rings for up to 5 s
        tone = sum(a * np.exp(-tt / tau) * np.sin(2 * np.pi * freq * r * tt + r) for r, a, tau in _BELL_PARTIALS)
        _bell_tones[freq] = tone * np.minimum(tt / 0.004, 1.0)  # 4 ms strike
    return _bell_tones[freq]

def _render_bells(np, notes: List[Tuple[float, float, float]], rate: int = 44100):
    end = max(t for t, _, _ in notes) + 5.0
    out = np.zeros(int(end * rate), dtype=np.float64)
    for start, freq, strength in notes:
        tone = _bell_tone(np, freq, rate)
        i = max(0, int((start - CHIME_LATENCY_SECONDS) * rate))
        n = min(len(tone), len(out) - i)
        out[i:i + n] += tone[:n] * strength
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * max(0.0, min(CHIME_VOLUME, 1.0)) * 32767).astype('<i2')

_chime_proc: Optional[subprocess.Popen] = None

def stop_chime(wait: bool = True) -> None:
    """Silences the bells if they're still ringing, and collects the player once it has exited.
    wait=False only collects a player that has already finished."""
    global _chime_proc
    if not _chime_proc:
        return
    if _chime_proc.poll() is None:
        if not wait:
            return
        _chime_proc.terminate()
        try: _chime_proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired: _chime_proc.kill(); _chime_proc.wait()
    _chime_proc = None

def play_chime(notes: List[Tuple[float, float, float]]) -> None:
    """Renders the bells and starts playing them now, in the background."""
    global _chime_proc
    import wave
    try:
        import numpy as np  # type: ignore
    except ImportError:
        logging.warning("Westminster: numpy missing, no bells"); return
    env = dict(os.environ)
    if CHIME_PLAYER:
        cmd = shlex.split(CHIME_PLAYER)
    else:
        # The service runs outside the desktop session, so point it at the user's PipeWire/Pulse.
        runtime = f"/run/user/{os.getuid()}" if hasattr(os, 'getuid') else ''
        env.setdefault('XDG_RUNTIME_DIR', runtime)
        if shutil.which('pw-play') and os.path.exists(f"{runtime}/pipewire-0"): cmd = ['pw-play']
        elif shutil.which('paplay') and os.path.exists(f"{runtime}/pulse/native"): cmd = ['paplay']
        elif shutil.which('aplay'): cmd = ['aplay', '-q']
        else:
            logging.warning("Westminster: no audio player found (pw-play, paplay or aplay)"); return
    path = os.path.join(tempfile.gettempdir(), 'traffic_light_chime.wav')
    try:
        pcm = _render_bells(np, notes)
        with wave.open(path, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(pcm.tobytes())
        stop_chime()
        _chime_proc = subprocess.Popen(cmd + [path], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logging.error(f"Westminster: playing the bells failed: {e}")

def _start_clock_chime(ms: Dict, bells: bool, quarter: int, hour12: int) -> None:
    if bells:
        ms['clock_seq'], notes = westminster_sequence(quarter, hour12)
        play_chime(notes)  # the light sequence starts right after, in step with the sound
    else:
        ms['clock_seq'] = clock_chime_sequence(quarter, hour12)
    ms['clock_idx'] = -1; ms['clock_step_until'] = time()  # the chime starts now (the bells too)

def handle_westminster_mode(controller, elapsed: float) -> Optional[float]:
    return handle_clock_mode(controller, elapsed, bells=True)

def handle_clock_mode(controller, elapsed: float, bells: bool = False) -> Optional[float]:
    ms = controller.state.mode_state; now = time(); dt = datetime.now()
    forced = ms.pop('clock_force', None)  # /?action=chime: ring this one now, to test
    if forced:
        ms['clock_last_key'] = (dt.date().isoformat(), dt.hour, dt.minute // 15)  # don't chime the real time on top
        _start_clock_chime(ms, bells, *forced)
        return 0.02
    seq = ms.get('clock_seq')
    if seq:
        if now < ms.get('clock_step_until', 0.0):
            return 0.02
        idx = ms.get('clock_idx', -1) + 1
        if idx < len(seq):
            color, dur = seq[idx]
            controller.set_light_state(color)
            # Each step ends a fixed time after the chime started (not after the previous step
            # actually switched), so tick delays don't add up and the lamps stay on the bells.
            ms['clock_idx'] = idx; ms['clock_step_until'] = ms.get('clock_step_until', now) + dur
            return 0.02
        ms['clock_seq'] = None
    stop_chime(wait=False)  # the last bells may still be ringing out; collect the player once done

    q = dt.minute // 15
    key = (dt.date().isoformat(), dt.hour, q)
    if ms.get('clock_last_key') != key:
        # Entering the mode (key is None) always chimes - that's how you ask the time.
        entering = ms.get('clock_last_key') is None
        ms['clock_last_key'] = key
        if entering or not _clock_quiet(dt.hour):
            _start_clock_chime(ms, bells, q or 4, dt.hour % 12 or 12)
            return 0.02
    controller.set_light_state('off')
    return 0.2

# --------------------------- Monitors --------------------------------------
def _poll(controller, name: str, fetch, interval: float, store) -> None:
    """Calls fetch() every interval and hands a good (non-empty) result to store() under the lock.
    A failed fetch keeps the last good value and retries soon (30 s, doubling up to the
    interval) - so a network hiccup, or Wi-Fi not being up yet at boot, doesn't blank a mode
    for a whole interval. Only when the value is POLL_STALE_INTERVALS intervals old is it
    dropped and the mode shows "no data"."""
    last_ok = 0.0; retry = POLL_RETRY_SECONDS; cleared = True
    while controller.state.running:
        try:
            value = fetch()
        except Exception as e:
            logging.warning(f"{name}: fetch failed: {e}"); value = None
        now = time()
        if value:
            with controller.state.lock: store(value)
            last_ok = now; retry = POLL_RETRY_SECONDS; cleared = False
            wait = interval
        else:
            if not cleared and now - last_ok > interval * POLL_STALE_INTERVALS:
                with controller.state.lock: store({})
                cleared = True
                logging.warning(f"{name}: no fresh data for {now - last_ok:.0f} s, dropped")
            wait = min(retry, interval); retry = min(retry * 2, interval)
            logging.info(f"{name}: no data, retrying in {wait:.0f} s")
        sleep(wait)

def _get_interface_ipv4(ifname: str) -> Optional[str]:
    """Return IPv4 address for an interface (Linux), or None."""
    try:
        import fcntl  # Linux only; elsewhere (UI work on a PC) there's no interface to ask
    except ImportError:
        return None
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

def _db_get(path: str) -> Optional[bytes]:
    headers = {"DB-Client-Id": DB_CLIENT_ID, "DB-Api-Key": DB_CLIENT_SECRET, "accept": "application/xml"}
    try:
        r = requests.get(f"{DB_API_URL}/{path}", headers=headers, timeout=API_TIMEOUT)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logging.debug(f"DB timetable {path} failed: {e}")
        return None

def _fetch_departures() -> Optional[List[datetime]]:
    """Upcoming departures in our direction, with live times and without cancelled trains.
    None if the timetable couldn't be fetched at all."""
    now = datetime.now(); stops = []; got_plan = False
    for i in range(2):  # this hour and the next
        t = now + timedelta(hours=i)
        content = _db_get(f"plan/{S_BAHN_EVA}/{t.strftime('%y%m%d')}/{t.strftime('%H')}")
        if content is None:
            continue  # keep the other hour
        got_plan = True
        if content:
            try: stops.extend(ET.fromstring(content).findall('s'))
            except ET.ParseError: pass
    if not got_plan:
        return None
    # Live changes for the station: new departure time (ct) and cancellation (cs="c"), matched by stop id.
    changes: Dict[str, ET.Element] = {}
    content = _db_get(f"fchg/{S_BAHN_EVA}")
    if content:
        try:
            for s in ET.fromstring(content).findall('s'):
                dp = s.find('dp')
                if dp is not None and s.get('id'):
                    changes[s.get('id')] = dp
        except ET.ParseError:
            pass
    departures = []
    for s in stops:
        dp = s.find('.//dp')
        if dp is None or not dp.get('ppth') or not dp.get('pt'):
            continue
        if dp.get('ppth').split('|')[-1] in OUTBOUND_DESTINATIONS:
            continue
        live = changes.get(s.get('id'))
        if live is not None and live.get('cs') == 'c':
            continue  # cancelled
        try:
            dt = datetime.strptime((live.get('ct') if live is not None and live.get('ct') else dp.get('pt')), '%y%m%d%H%M')
        except ValueError:
            continue
        departures.append(dt)
    return sorted(departures)

def s_bahn_monitor(controller):
    if not (DB_CLIENT_ID and DB_CLIENT_SECRET and S_BAHN_EVA):
        logging.warning("S-Bahn disabled: credentials or S_BAHN_EVA missing")
        return
    departures: List[datetime] = []; fetched_at = 0.0
    while controller.state.running:
        fresh = _fetch_departures()
        if fresh is not None:
            departures, fetched_at = fresh, time()
        elif time() - fetched_at > S_BAHN_STALE_SECONDS:
            departures = []  # too old to trust (cancellations, delays)
        # Minutes from the stored times, so a failed fetch doesn't freeze or blank the countdown.
        now = datetime.now()
        upcoming = [int((d - now).total_seconds() / 60) for d in departures if d >= now]
        with controller.state.lock:
            controller.state.s_bahn_minutes_away = min(upcoming) if upcoming else -1
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
    _poll(controller, 'Transit', _fetch_transit_commute, TRANSIT_POLL_INTERVAL,
          lambda v: setattr(controller.state, 's_bahn_transit_status', v))

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
        kp = int(round(float(raw_kp)))  # NOAA gives thirds (4.67 = "5-"); truncating under-reports storms
        cond = 'Storm' if kp >= 5 else ('Active' if kp == 4 else 'Quiet')
        return {'kp_index': kp, 'condition': cond}
    except Exception as e:
        logging.debug(f"Space weather fetch failed: {e}")
        return {}

def space_weather_monitor(controller):
    _poll(controller, 'Space weather', _fetch_space, SPACE_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'space_weather_status', v))

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
    _poll(controller, 'Traffic', _fetch_traffic, TRAFFIC_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'traffic_status', v))

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

def _rain_outlook(current: Dict, hourly: Dict) -> Dict:
    """Raining now, and minutes until the first rainy hour within the look-ahead."""
    times = hourly.get('time') or []
    mm = hourly.get('precipitation') or []
    prob = hourly.get('precipitation_probability') or []
    if not times:
        return {}
    now = datetime.now()
    eta: Optional[int] = None; prob_max = 0
    for i, ts in enumerate(times):
        try:
            start = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if start + timedelta(hours=1) <= now:
            continue  # already over
        minutes = max(0, int((start - now).total_seconds() // 60))
        if minutes > RAIN_YELLOW_MINUTES:
            break
        p = prob[i] if i < len(prob) and isinstance(prob[i], (int, float)) else 0
        m = mm[i] if i < len(mm) and isinstance(mm[i], (int, float)) else 0.0
        prob_max = max(prob_max, int(p))
        if eta is None and (m >= RAIN_MM or p >= RAIN_PROBABILITY):
            eta = minutes
    now_mm = current.get('precipitation')
    return {
        'rain_now': bool(isinstance(now_mm, (int, float)) and now_mm >= RAIN_MM / 2),
        'rain_eta_min': eta,
        'rain_prob_max': prob_max,
    }

def _wmo_condition(code: int) -> str:
    """Open-Meteo's WMO weather code as a one-word condition."""
    if code <= 1: return 'Clear'
    if code <= 3: return 'Clouds'
    if code in (45, 48): return 'Fog'
    if 51 <= code <= 57: return 'Drizzle'
    if 71 <= code <= 77 or code in (85, 86): return 'Snow'
    if code >= 95: return 'Thunderstorm'
    return 'Rain'  # 61-67, 80-82

def _fetch_environment() -> Dict:
    result: Dict = {}
    # Forecast: UV index + is_day + sunrise/sunset
    try:
        r = requests.get(
            OPEN_METEO_FORECAST_URL,
            params={
                'latitude': WEATHER_LAT,
                'longitude': WEATHER_LON,
                'current': 'uv_index,is_day,temperature_2m,apparent_temperature,precipitation,weather_code',
                'daily': 'sunrise,sunset,temperature_2m_max',
                'hourly': 'precipitation,precipitation_probability',
                'timezone': 'auto',
                'forecast_days': 2,  # so the 3-hour rain look-ahead works late in the evening
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
        for key, src in (('temp', 'temperature_2m'), ('feels_like', 'apparent_temperature')):
            if isinstance(cur.get(src), (int, float)):
                result[key] = round(float(cur[src]), 1)
        if isinstance(cur.get('weather_code'), (int, float)):
            result['condition'] = _wmo_condition(int(cur['weather_code']))
        highs = daily.get('temperature_2m_max') or []
        if highs and isinstance(highs[0], (int, float)):
            result['temp_max'] = round(float(highs[0]), 1)
        result.update(_rain_outlook(cur, data.get('hourly') or {}))
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
    # Forecast and air quality are separate requests; when only one comes back,
    # merge it over the last values instead of losing the other half.
    _poll(controller, 'Environment', _fetch_environment, ENVIRONMENT_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'environment_status', {**controller.state.environment_status, **v} if v else {}))

# Grêmio last match result and next match via ESPN free API (no key required)

def _espn_competitor_score(competitor: Dict) -> int:
    s = competitor.get('score')
    if isinstance(s, dict):
        return int(float(s.get('displayValue') or 0))
    return int(float(s or 0))

def _fetch_gremio_last_result() -> Dict:
    out: Dict = {}

    # Last completed match, across every tracked competition: newest completed
    # event per league (sorted here rather than trusting ESPN's order), then the
    # newest of those.
    best_ev, best_competitors = None, None
    for league in GREMIO_ESPN_LEAGUES:
        try:
            r = requests.get(_gremio_schedule_url(league), timeout=API_TIMEOUT)
            r.raise_for_status()
            events = sorted(r.json().get('events') or [], key=lambda e: e.get('date') or '', reverse=True)
            for ev in events:
                comp = (ev.get('competitions') or [{}])[0]
                if not (comp.get('status') or {}).get('type', {}).get('completed'):
                    continue
                if best_ev is None or (ev.get('date') or '') > (best_ev.get('date') or ''):
                    best_ev, best_competitors = ev, comp.get('competitors') or []
                break
        except Exception as e:
            logging.debug(f"Grêmio ESPN schedule fetch failed ({league}): {e}")

    if best_ev is not None:
        gremio_c = next((c for c in best_competitors if 'grêmio' in (c.get('team', {}).get('displayName') or '').lower()), None)
        opp_c = next((c for c in best_competitors if c is not gremio_c), None)
        if gremio_c and opp_c:
            gs = _espn_competitor_score(gremio_c)
            os_ = _espn_competitor_score(opp_c)
            # ESPN's winner flag also covers penalty shootouts, where the goals are level.
            if gremio_c.get('winner'):
                match_result = 'win'
            elif opp_c.get('winner') or gs < os_:
                match_result = 'loss'
            else:
                match_result = 'draw'
            out.update({
                'result': match_result,
                'score': f"{gs}-{os_}",
                'opponent': (opp_c.get('team') or {}).get('displayName', '?'),
                'home_away': 'home' if gremio_c.get('homeAway') == 'home' else 'away',
                'date': best_ev.get('date') or '',
                'competition': (best_ev.get('league') or {}).get('name'),
            })

    # Next upcoming match, across every tracked competition: each league's team
    # fixtures (the scoreboard's date-range query answers 400).
    best_next, best_next_competitors = None, None
    for league in GREMIO_ESPN_LEAGUES:
        try:
            r = requests.get(_gremio_schedule_url(league), params={'fixture': 'true'}, timeout=API_TIMEOUT)
            r.raise_for_status()
            events = sorted(r.json().get('events') or [], key=lambda e: e.get('date') or '')
            for ev in events:
                comp = (ev.get('competitions') or [{}])[0]
                if (comp.get('status') or {}).get('type', {}).get('state') != 'pre':
                    continue  # already played, or under way
                if best_next is None or (ev.get('date') or '') < (best_next.get('date') or ''):
                    best_next, best_next_competitors = ev, comp.get('competitors') or []
                break
        except Exception as e:
            logging.debug(f"Grêmio ESPN next match fetch failed ({league}): {e}")

    if best_next is not None:
        gremio_c = next((c for c in best_next_competitors if 'grêmio' in (c.get('team', {}).get('displayName') or '').lower()), None)
        opp_c = next((c for c in best_next_competitors if c is not gremio_c), None) if gremio_c else None
        out['next_match_date'] = best_next.get('date', '')
        out['next_match_opponent'] = (opp_c.get('team') or {}).get('displayName', '?') if opp_c else '?'
        out['next_match_home_away'] = 'home' if (gremio_c or {}).get('homeAway') == 'home' else 'away'

    # Without a last result the mode has nothing to show; count it as a failed fetch.
    return out if 'result' in out else {}

def gremio_monitor(controller):
    _poll(controller, 'Grêmio', _fetch_gremio_last_result, GREMIO_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'gremio_status', v))

class BeatTracker:
    """Live tempo/phase/energy estimate from mic blocks (see PARTY_* config)."""

    def __init__(self, np, samplerate: int, blocksize: int):
        self.np = np; self.sr = samplerate; self.bs = blocksize
        self.fps = samplerate / blocksize  # onset envelope frames per second
        self.window = np.hanning(2 * blocksize)
        freqs = np.fft.rfftfreq(2 * blocksize, 1.0 / samplerate)
        self.bass = (freqs >= PARTY_BASS_BAND[0]) & (freqs <= PARTY_BASS_BAND[1])
        self.mid = (freqs > PARTY_MID_BAND[0]) & (freqs <= PARTY_MID_BAND[1])
        self.lag_min = max(1, int(self.fps * 60.0 / PARTY_MAX_BPM))
        self.lag_max = int(math.ceil(self.fps * 60.0 / PARTY_MIN_BPM))
        lags = np.arange(self.lag_max + 2, dtype=float); lags[0] = 1.0
        # Log-normal tempo preference: halves/doubles of the beat get less weight.
        self.prior = np.exp(-0.5 * (np.log2((60.0 * self.fps / lags) / PARTY_PRIOR_BPM) / PARTY_PRIOR_OCTAVES) ** 2)
        self.maxlen = int(PARTY_TEMPO_WINDOW_SECONDS * self.fps)
        self.reset()

    def reset(self) -> None:
        np = self.np
        self.prev_block = np.zeros(self.bs, dtype=np.float32)
        self.prev_bass = None; self.prev_mid = None
        self.mid_avg = 0.0
        self.env: deque = deque(maxlen=self.maxlen); self.env_t: deque = deque(maxlen=self.maxlen)
        self.bpm: Optional[float] = None; self.confidence = 0.0
        self.anchor: Optional[float] = None
        self.pending_bpm: Optional[float] = None; self.pending_count = 0
        self.weak_count = 0
        self.last_tempo_update = 0.0
        self.short_db: Optional[float] = None; self.long_db: Optional[float] = None
        self.band_mean = [0.0, 0.0]; self.band_var = [0.0, 0.0]; self.band_n = [0, 0]
        self.last_hit = 0.0; self.hit_seq = 0; self.hit_strength = 0.0

    def _band_z(self, i: int, x: float) -> float:
        # Score against the baseline *before* this block, then fold it in.
        mean, var = self.band_mean[i], self.band_var[i]
        z = (x - mean) / math.sqrt(var) if self.band_n[i] >= self.fps and var > 1e-12 else 0.0
        a = 1.0 - math.exp(-1.0 / (PARTY_HIT_TAU_SECONDS * self.fps))
        d = x - mean
        self.band_mean[i] = mean + a * d
        self.band_var[i] = (1.0 - a) * (var + a * d * d)
        self.band_n[i] += 1
        return z

    def process(self, samples, now: float, mask_mid: bool) -> None:
        np = self.np
        frame = np.concatenate((self.prev_block, samples)) * self.window
        self.prev_block = samples.copy()
        mag = np.log1p(1000.0 * np.abs(np.fft.rfft(frame)))
        bass = mag[self.bass]; mid = mag[self.mid]
        bass_flux = float(np.mean(np.maximum(0.0, bass - self.prev_bass))) if self.prev_bass is not None else 0.0
        hit_z = self._band_z(0, bass_flux)
        if mask_mid or self.prev_mid is None:
            mid_flux = self.mid_avg  # relay click is ringing: hold the mid band at its average
        else:
            mid_flux = float(np.mean(np.maximum(0.0, mid - self.prev_mid)))
            self.mid_avg += (mid_flux - self.mid_avg) * 0.05
            hit_z = max(hit_z, self._band_z(1, mid_flux))
        # Per-band z-score so whichever band carries the attacks (bass for a
        # ballad, mids for guitars/snare) can trigger, whatever its level.
        if hit_z >= PARTY_HIT_Z and now - self.last_hit >= PARTY_MIN_SWITCH_SECONDS:
            self.last_hit = now; self.hit_seq += 1; self.hit_strength = hit_z
        self.prev_bass = bass; self.prev_mid = mid
        # The block was captured over the last bs/sr seconds; stamp its middle.
        self.env.append(bass_flux + PARTY_MID_WEIGHT * mid_flux)
        self.env_t.append(now - 0.5 * self.bs / self.sr)

        if not mask_mid:  # the click would read as loudness too
            self._update_energy(float(np.sqrt(np.mean(np.square(samples)))))
        if now - self.last_tempo_update >= PARTY_TEMPO_UPDATE_SECONDS:
            self.last_tempo_update = now
            if not self._music():
                # In a quiet room the only rhythm left is our own relay
                # clicks - don't let them become a phantom beat.
                self.bpm = None; self.anchor = None; self.confidence = 0.0
                return
            self._update_tempo()
            self._update_phase()

    def _update_energy(self, rms: float) -> None:
        db = 20.0 * math.log10(rms + 1e-9)
        dt = self.bs / self.sr
        if self.short_db is None:
            self.short_db = self.long_db = db
        self.short_db += (db - self.short_db) * (1.0 - math.exp(-dt / 0.4))
        self.long_db += (db - self.long_db) * (1.0 - math.exp(-dt / 15.0))

    def _onset_curve(self):
        # Subtract a ~0.5 s moving average and keep what sticks out, so the
        # tempo search sees the hits and not the overall loudness.
        np = self.np
        e = np.asarray(self.env, dtype=float)
        k = max(3, int(self.fps * 0.5)) | 1
        local = np.convolve(e, np.ones(k) / k, mode='same')
        return np.maximum(0.0, e - local)

    def _acf_tempo(self) -> Optional[Tuple[float, float]]:
        """(bpm, confidence) of the strongest beat period in an onset envelope."""
        np = self.np
        e = self._onset_curve(); e = e - e.mean()
        n = len(e); size = 1 << (2 * n - 1).bit_length()
        spec = np.fft.rfft(e, size)
        acf = np.fft.irfft(spec * np.conj(spec), size)[:n]
        if acf[0] <= 1e-12:
            return None
        acf = acf / acf[0]
        hi = min(self.lag_max, n // 2)
        if hi <= self.lag_min + 1:
            return None
        lags = np.arange(self.lag_min, hi + 1)
        # A true beat period also lines up at twice the lag; 8th notes don't
        # line up as well at their own double, which helps pick the beat.
        double = np.where(2 * lags < n, acf[np.minimum(2 * lags, n - 1)], 0.0)
        score = (acf[lags] + 0.5 * np.maximum(0.0, double)) * self.prior[lags]
        i = int(np.argmax(score)); lag = float(lags[i]); conf = float(acf[lags[i]])
        if 0 < i < len(lags) - 1:  # parabolic peak refinement on the raw autocorrelation
            a, b, c = acf[lags[i] - 1], acf[lags[i]], acf[lags[i] + 1]
            den = a - 2 * b + c
            if abs(den) > 1e-12:
                lag += max(-0.5, min(0.5, float(0.5 * (a - c) / den)))
        return 60.0 * self.fps / lag, conf  # plain floats: the status dict goes out as JSON

    def _update_tempo(self) -> None:
        if len(self.env) < PARTY_MIN_HISTORY_SECONDS * self.fps:
            return
        found = self._acf_tempo()
        if found is None:
            self._weak(); return
        raw, conf = found
        self.confidence = conf
        if conf < PARTY_LOCK_CONFIDENCE:
            self._weak(); return
        self.weak_count = 0
        if self.bpm is None:
            self.bpm = raw; self.pending_bpm = None
            logging.info(f"Party: locked {raw:.1f} BPM (conf {conf:.2f})")
            return
        ratio = raw / self.bpm
        if abs(ratio - 1.0) <= 0.06:
            self.bpm += (raw - self.bpm) * 0.2; self.pending_bpm = None
        elif abs(ratio - 2.0) <= 0.12 or abs(ratio - 0.5) <= 0.03:
            pass  # octave flip of the same tempo - keep what we have
        elif self.pending_bpm and abs(raw / self.pending_bpm - 1.0) <= 0.06:
            self.pending_count += 1
            if self.pending_count >= PARTY_TEMPO_CHANGE_UPDATES:
                logging.info(f"Party: tempo change {self.bpm:.1f} -> {raw:.1f} BPM")
                self.bpm = raw; self.pending_bpm = None; self.anchor = None
        else:
            self.pending_bpm = raw; self.pending_count = 1

    def _weak(self) -> None:
        self.weak_count += 1
        if self.bpm is not None and self.weak_count >= PARTY_UNLOCK_UPDATES:
            logging.info("Party: lost the beat")
            self.bpm = None; self.anchor = None

    def _update_phase(self) -> None:
        if not self.bpm:
            return
        np = self.np
        e = self._onset_curve(); n = len(e)
        period = self.fps * 60.0 / self.bpm
        beats = int((n - 1) / period)
        if beats < 2:
            return
        # Comb filter: for each offset back from the newest frame, sum the
        # envelope at every beat before it (recent beats weigh more).
        best, best_off = -1.0, 0
        weights = 0.85 ** np.arange(beats)
        for off in range(int(math.ceil(period))):
            idx = (n - 1 - off - np.round(np.arange(beats) * period)).astype(int)
            idx = idx[idx >= 0]
            sc = float(np.dot(e[idx], weights[:len(idx)]))
            if sc > best:
                best, best_off = sc, off
        found = self.env_t[n - 1 - best_off]
        p_sec = 60.0 / self.bpm
        if self.anchor is None:
            self.anchor = found; return
        # Phase-locked loop: nudge toward the new estimate instead of jumping,
        # so one sloppy estimate doesn't jerk the lights.
        projected = self.anchor + round((found - self.anchor) / p_sec) * p_sec
        err = found - projected
        self.anchor = projected + 0.35 * err

    def _music(self) -> bool:
        return self.short_db is not None and self.short_db > 20.0 * math.log10(PARTY_SILENCE_RMS)

    def status(self, now: float) -> Dict:
        silence_db = 20.0 * math.log10(PARTY_SILENCE_RMS)
        music = self._music()
        if not music:
            vibe = 'silence'
        elif not self.bpm or self.anchor is None:
            vibe = 'ambient'
        else:
            # Label only (shown in the UI) - the lights hop on every beat either way.
            vibe = 'chill' if self.bpm < PARTY_CHILL_MAX_BPM else 'groove' if self.bpm < PARTY_GROOVE_MAX_BPM else 'hype'
        level = 0.0 if self.short_db is None else max(0.0, min(1.0, (self.short_db - silence_db) / 30.0))
        return {
            'bpm': round(self.bpm, 1) if self.bpm else None,
            'confidence': round(self.confidence, 2),
            'beat_anchor': self.anchor if self.bpm else None,
            'vibe': vibe,
            'level': round(level, 2),
            'db': round(self.short_db, 1) if self.short_db is not None else None,
            'rel_db': round(self.short_db - self.long_db, 1) if self.short_db is not None else None,
            'hit_seq': self.hit_seq,
            'hit_strength': round(self.hit_strength, 1),
        }

def _update_vu_level(controller, level: float) -> None:
    now = time()
    with controller.state.lock:
        # The relay clicks audibly when the light switches - drop samples
        # taken while that click is still ringing out, or the mic picks up
        # its own relay and re-triggers another switch in a feedback loop.
        ms = controller.state.mode_state
        if now < float(ms.get('audio_vu_ignore_until', 0.0) or 0.0):
            return
        floor = float(ms.get('audio_vu_floor', AUDIO_VU_FLOOR_SEED) or AUDIO_VU_FLOOR_SEED)
        last_update = float(ms.get('audio_vu_last_update', now) or now)
        dt = max(0.0, min(now - last_update, 0.5))
        ms['audio_vu_last_update'] = now

        # Asymmetric envelope follower: floor snaps down onto quiet fast
        # but only creeps up slowly - so it settles on "recent quiet",
        # not an average that a sustained loud stretch would drag upward.
        floor_tau = AUDIO_VU_FLOOR_TAU_DOWN if level < floor else AUDIO_VU_FLOOR_TAU_UP
        floor += (level - floor) * (1.0 - math.exp(-dt / floor_tau))
        ms['audio_vu_floor'] = floor

        ceiling = max(floor * AUDIO_VU_CEILING_RATIO, floor + AUDIO_VU_MIN_RANGE)
        target = max(0.0, min((level - floor) / (ceiling - floor), 1.0))
        prev = controller.state.audio_vu_level
        smoothing = AUDIO_VU_SMOOTHING_ATTACK if target >= prev else AUDIO_VU_SMOOTHING_RELEASE
        controller.state.audio_vu_level = max(0.0, (prev * smoothing) + (target * (1.0 - smoothing)))
        ms['audio_vu_last'] = now

def _save_party_recording(np, blocks: List, samplerate: int) -> None:
    import wave
    try:
        os.makedirs(PARTY_RECORD_DIR, exist_ok=True)
        path = os.path.join(PARTY_RECORD_DIR, datetime.now().strftime("live-%Y%m%d-%H%M%S.wav"))
        pcm = (np.clip(np.concatenate(blocks), -1.0, 1.0) * 32767).astype('<i2')
        with wave.open(path, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(samplerate); w.writeframes(pcm.tobytes())
        logging.info(f"Party: saved {len(pcm) / samplerate:.1f}s to {path}")
    except Exception as e:
        logging.error(f"Party: recording failed: {e}")

def audio_monitor(controller):
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        logging.warning("Party audio disabled: install sounddevice and numpy")
        return

    blocksize = 1024

    def _resolve_input_device() -> Tuple[Optional[int], Optional[str]]:
        try:
            devices = sd.query_devices()
        except Exception as e:
            logging.error(f"Audio: failed to query devices: {e}")
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
                logging.warning(f"Audio: AUDIO_INPUT_DEVICE index {idx} is not a valid input device")
            else:
                wanted = configured.lower()
                for idx, dev in enumerate(devices):
                    name = str(dev.get('name', ''))
                    if wanted in name.lower() and _is_valid_input(idx):
                        return idx, name
                logging.warning(f"Audio: AUDIO_INPUT_DEVICE '{configured}' not found among input devices")

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

    def _candidate_sample_rates(device_index: int) -> List[int]:
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

    def _listen(stream, samplerate: int, device_index: int, device_name: str) -> None:
        """Reads the mic until shutdown; raises if the stream fails (mic unplugged)."""
        tracker = BeatTracker(np, samplerate, blocksize)
        active = False; last_log = 0.0
        recent: deque = deque(maxlen=int(PARTY_RECORD_SECONDS * samplerate / blocksize))
        logging.info(
            f"Audio monitor started (device={device_index}: {device_name}, rate={samplerate}Hz)"
        )
        while controller.state.running:
            data, overflowed = stream.read(blocksize)
            if overflowed:
                logging.debug(f"Audio input overflow: {overflowed}")
            if data.size == 0:
                continue
            samples = data[:, 0] if data.ndim > 1 else data
            now = time()
            with controller.state.lock:
                mode = controller.state.current_mode
                party = mode == 'party'
                click = now < controller.state.mode_state.get('click_until', 0.0)
                if not party and active:
                    controller.state.party_status = {}
            if mode == 'audio_vu':
                rms = float(np.sqrt(np.mean(np.square(samples))))
                peak = float(np.max(np.abs(samples)))
                _update_vu_level(controller, max(rms, peak * 0.35))
            # Only analyse while Party is on; the stream keeps running
            # so the mic doesn't need re-opening every time.
            if not party:
                active = False
                continue
            if not active:
                tracker.reset(); active = True; recent.clear()
            recent.append(samples.copy())
            if controller.state.mode_state.pop('party_record', False):
                _save_party_recording(np, recent, samplerate)
            try:
                tracker.process(samples, now, mask_mid=click)
                status = tracker.status(now)
            except Exception as e:
                # Keep the stream alive; start the analysis over.
                logging.error(f"Party tracker error: {e}")
                tracker.reset(); continue
            with controller.state.lock:
                controller.state.party_status = status
            if now - last_log >= PARTY_LOG_INTERVAL:
                last_log = now
                logging.info(
                    f"Party: vibe={status['vibe']} bpm={status['bpm']} conf={status['confidence']} "
                    f"db={status['db']} rel={status['rel_db']}"
                )

    # Find the mic and open it; if it's missing or fails later (unplugged), look
    # again with a growing pause instead of leaving Party and VU dead until a restart.
    retry = AUDIO_RETRY_SECONDS
    try:
        while controller.state.running:
            device_index, device_name = _resolve_input_device()
            if device_index is None:
                logging.warning("Audio: no input-capable audio device found")
            else:
                last_error: Optional[Exception] = None
                tried_rates = _candidate_sample_rates(device_index)
                for samplerate in tried_rates:
                    try:
                        stream = sd.InputStream(device=device_index, channels=1, samplerate=samplerate,
                                                blocksize=blocksize, dtype='float32', latency='low')
                    except Exception as e:
                        last_error = e
                        logging.warning(f"Audio: failed opening InputStream at {samplerate}Hz on device {device_index} ({device_name}): {e}")
                        continue
                    try:
                        with stream:
                            retry = AUDIO_RETRY_SECONDS
                            _listen(stream, samplerate, device_index, str(device_name))
                    except Exception as e:
                        logging.error(f"Audio: stream on {device_name} stopped: {e}")
                    break
                else:
                    logging.error(f"Audio: unable to open device {device_index} ({device_name}), tried rates={tried_rates}. Last error: {last_error}")
            if not controller.state.running:
                break
            with controller.state.lock:
                controller.state.party_status = {}
            logging.info(f"Audio: looking for the mic again in {retry:.0f} s")
            sleep(retry); retry = min(retry * 2, AUDIO_RETRY_MAX_SECONDS)
            try:  # PortAudio only sees a re-plugged device after a rescan
                sd._terminate(); sd._initialize()
            except Exception:
                pass
    finally:
        with controller.state.lock:
            controller.state.party_status = {}

# SimHub UDP

def network_monitor(controller):
    # A Wi-Fi drop switches to the traffic light cycle; when it's back, the mode
    # picked before the drop returns.
    with controller.state.lock:
        prev: Optional[bool] = controller.state.network_connected  # main() already set the mode for this
    while controller.state.running:
        try:
            connected = _is_network_connected()
        except Exception:
            connected = False
        with controller.state.lock:
            controller.state.network_connected = connected
            if connected != prev:
                if connected:
                    controller.restore_choice()
                    logging.info(f"Network up -> back to {controller.state.target_mode}")
                else:
                    controller.state.target_mode = 'auto'
                    logging.info("Network down -> target auto")
                prev = connected
        sleep(NETWORK_POLL_INTERVAL)

def simhub_udp_listener(controller):
    valid = {'red','yellow','green','black','green-yellow','all_on'}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((SIMHUB_UDP_HOST, SIMHUB_UDP_PORT)); sock.settimeout(1.0)
            while controller.state.running:
                try:
                    data, _ = sock.recvfrom(1024)
                    payload = data.decode('utf-8').strip()
                    # Wire format is "<rev_color>[:<revpct>[:<delta>:<invalid>[:<flag_color>]]]";
                    # the extra fields are optional so older senders keep working.
                    # delta = seconds vs your best lap (empty = no reference yet),
                    # invalid = 1 once the current lap is invalidated,
                    # flag_color = race start lights / flags (old senders put flags in the first field).
                    fields = payload.split(':')
                    color = fields[0]
                    if color in valid:
                        def num(i, lo=None, hi=None):
                            try:
                                v = float(fields[i])
                            except (IndexError, ValueError):
                                return None
                            return v if lo is None else max(lo, min(hi, v))
                        with controller.state.lock:
                            controller.state.simhub_light_status = color
                            controller.state.simhub_rev_pct = num(1, 0.0, 100.0)
                            controller.state.simhub_delta = num(2)
                            controller.state.simhub_lap_invalid = len(fields) > 3 and fields[3] == '1'
                            controller.state.simhub_flag_status = fields[4] if len(fields) > 4 and fields[4] in valid else color
                            controller.state.simhub_last_rx = time()
                except socket.timeout: continue
                except Exception: continue
    except Exception as e:
        logging.error(f"SimHub listener error: {e}")

# --------------------------- Web Server ------------------------------------
_HTML = f"""
    <!DOCTYPE html><html lang="en"><head><title>Traffic Light Control</title><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover"><meta name="theme-color" content="#0d0f13"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Traffic Light">
    <style>
    :root{{--page-bg-1:#0d0f13;--page-bg-2:#161a22;--surface:#15171b;--panel-border:rgba(255,255,255,.08);--text-color:#eef0f2;--text-muted:#8b909a;--ok:#00e08a;--warn:#ffb020}}
    *{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
    html,body{{height:100%;margin:0;padding:0;background:var(--page-bg-2);overscroll-behavior:none}}
    body{{min-height:100vh;min-height:-webkit-fill-available;min-height:100dvh;background:radial-gradient(1200px 600px at 50% -10%,rgba(47,155,255,.10),transparent 60%),linear-gradient(180deg,var(--page-bg-1),var(--page-bg-2));font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:var(--text-color);display:flex;justify-content:center;padding:max(env(safe-area-inset-top),20px) 16px max(env(safe-area-inset-bottom),24px)}}
    .container{{width:100%;max-width:380px;display:flex;flex-direction:column;gap:20px}}
    .topbar{{display:flex;align-items:center;justify-content:space-between;padding:2px 4px}}
    .brand{{font-weight:700;font-size:1.05em;letter-spacing:-.01em}}
    .conn-pill{{display:flex;align-items:center;gap:6px;font-size:.78em;font-weight:600;color:var(--text-muted);transition:color .25s}}
    .conn-dot{{width:7px;height:7px;border-radius:50%;background:var(--ok)}}
    .conn-pill.live{{color:#7fbf9f}}
    .conn-pill.lost{{color:#ffd9a0}}
    .conn-pill.lost .conn-dot{{background:var(--warn);animation:pulse 1s infinite}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .glass-card{{background:var(--surface);border:1px solid var(--panel-border);border-radius:18px;padding:18px 18px 14px;display:flex;flex-direction:column;align-items:center;gap:10px}}
    .traffic-light-body{{position:relative;padding:18px 20px;display:flex;flex-direction:column;gap:14px;border-radius:20px;border:1px solid transparent}}
    .light{{position:relative;width:86px;height:86px;border-radius:50%;background:#2a2d33;opacity:.4;transition:all .18s cubic-bezier(.4,0,.2,1);cursor:pointer;touch-action:manipulation;box-shadow:inset 0 2px 10px rgba(0,0,0,.5)}}
    .red-on{{background:#ff2b2b;opacity:1;box-shadow:0 0 16px 2px rgba(255,43,43,.55),inset 0 2px 10px rgba(0,0,0,.4)}}
    .yellow-on{{background:#ffcb2e;opacity:1;box-shadow:0 0 16px 2px rgba(255,203,46,.5),inset 0 2px 10px rgba(0,0,0,.4)}}
    .green-on{{background:#2bd672;opacity:1;box-shadow:0 0 16px 2px rgba(43,214,114,.5),inset 0 2px 10px rgba(0,0,0,.4)}}
    .controls{{text-align:center;width:100%}}
    #modeText{{font-size:.72em;font-weight:700;letter-spacing:.12em;color:var(--text-muted);text-transform:uppercase;margin:0}}
    #modeText strong{{display:block;font-size:1.6em;letter-spacing:0;color:var(--text-color)}}
    .info-text{{height:2.7em;line-height:1.35em;font-size:.92em;color:var(--text-muted);margin-top:4px;overflow:hidden}}
    .section-label{{font-size:.72em;font-weight:700;letter-spacing:.1em;color:var(--text-muted);text-transform:uppercase;margin:0 4px}}
    .mode-buttons{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;width:100%}}
    .mode-buttons a{{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 4px 10px;background:rgba(255,255,255,.045);border:1px solid var(--panel-border);border-radius:16px;color:var(--text-muted);text-decoration:none;touch-action:manipulation;transition:background .15s,transform .08s,color .15s,border-color .15s}}
    .mode-buttons a:active{{transform:scale(.94)}}
    .mode-buttons a svg{{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}}
    .mode-buttons a span{{font-size:.68em;font-weight:600;letter-spacing:.01em}}
    .mode-buttons a.active{{background:rgba(255,255,255,.06);border-color:#4a4d55;color:#f1f2f4}}
    #modeText strong em{{font-style:normal;color:var(--text-muted);font-weight:600}}
    .subs{{width:100%;height:76px;display:flex;flex-direction:column;gap:6px;padding-top:9px;border-top:1px solid var(--panel-border)}}
    .subs.empty{{visibility:hidden}}
    .chips{{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;flex:none}}
    .chips::-webkit-scrollbar{{display:none}}
    .chips .chip:first-child{{margin-left:auto}}
    .chips .chip:last-child{{margin-right:auto}}
    .chip{{flex:none;font:600 .76em -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:var(--text-muted);background:rgba(255,255,255,.045);border:1px solid var(--panel-border);border-radius:999px;padding:5px 10px;cursor:pointer;touch-action:manipulation}}
    .chip.active{{color:var(--text-color);background:rgba(255,255,255,.09);border-color:#4a4d55}}
    .rule{{flex:none;display:flex;justify-content:center;align-content:flex-start;gap:0 12px;flex-wrap:wrap;font-size:.72em;line-height:1.4;color:var(--text-muted);height:2.8em;overflow:hidden}}
    .rule span{{display:flex;align-items:center;gap:5px}}
    .rule b{{width:8px;height:8px;border-radius:50%;flex:none}}
    .stage{{position:relative;width:fit-content;margin:0 auto}}
    .led-strip{{position:absolute;top:0;bottom:0;left:100%;margin-left:14px;width:16px;display:flex;flex-direction:column-reverse;gap:4px;padding:2px;opacity:0;pointer-events:none;transition:opacity .15s}}
    .led-strip.visible{{opacity:1}}
    .led-seg{{flex:1 1 0;border-radius:2px;background:#2a2d33;transition:all .12s}}
    .gremio-stars{{position:absolute;top:-16px;left:50%;transform:translateX(-50%);display:none;gap:8px;align-items:center;justify-content:center;pointer-events:none}}
    .gremio-stars.visible{{display:flex}}
    .gremio-stars svg{{width:12px;height:12px}}
    .traffic-light-body.gremio-theme{{background:#1F1A17;border-color:#0D80BF}}
    .light.gremio-win-on{{background-color:#0D80BF;opacity:1;box-shadow:0 0 18px #0D80BF,inset 0 2px 10px rgba(0,0,0,.4)}}
    .gremio-face{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:86%;height:86%;opacity:0;pointer-events:none;transition:opacity .15s}}
    .gremio-face.visible{{opacity:1}}
    </style></head>
    <body><div class="container">
    <div class="topbar"><div class="brand">Traffic Light</div><div class="conn-pill live" id="connPill"><div class="conn-dot"></div><span id="connLabel">Live</span></div></div>
    <div class="glass-card">
    <div class="stage"><div class="gremio-stars" id="gremioStars"><svg viewBox="0 0 24 24" fill="#93754F"><path d="M12 2l2.9 6.6L22 9.3l-5 4.8 1.3 7.1L12 17.8 5.7 21.2 7 14.1 2 9.3l7.1-.7z"/></svg><svg viewBox="0 0 24 24" fill="#ABAAAA"><path d="M12 2l2.9 6.6L22 9.3l-5 4.8 1.3 7.1L12 17.8 5.7 21.2 7 14.1 2 9.3l7.1-.7z"/></svg><svg viewBox="0 0 24 24" fill="#F8C300"><path d="M12 2l2.9 6.6L22 9.3l-5 4.8 1.3 7.1L12 17.8 5.7 21.2 7 14.1 2 9.3l7.1-.7z"/></svg></div><div class="traffic-light-body" id="traffic-light"><div id="red" class="light" onclick="handleLightClick('red')"><svg class="gremio-face" id="gremioFaceRed" viewBox="0 0 40 40"></svg></div><div id="yellow" class="light" onclick="handleLightClick('yellow')"><svg class="gremio-face" id="gremioFaceYellow" viewBox="0 0 40 40"></svg></div><div id="green" class="light" onclick="handleLightClick('green')"><svg class="gremio-face" id="gremioFaceGreen" viewBox="0 0 40 40"></svg></div></div><div class="led-strip" id="ledStrip"></div></div>
    <div class="controls"><h2 id="modeText"><strong></strong></h2><div id="info-display" class="info-text"></div></div>
    <div class="subs empty" id="subs"><div class="chips" id="chips"></div><div class="rule" id="rule"></div></div>
    </div>
    <div class="section-label">Modes</div>
    <div class="mode-buttons">
    <a href="#" id="group-auto" onclick="event.preventDefault(); handleGroupClick('auto')"><svg viewBox="0 0 24 24"><rect x="8" y="2.5" width="8" height="19" rx="3"/><circle cx="12" cy="7" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="17" r="1.6"/></svg><span>Traffic light</span></a>
    <a href="#" id="group-clock" onclick="event.preventDefault(); handleGroupClick('clock')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 4.6v1.2M19.4 12h-1.2M12 19.4v-1.2M4.6 12h1.2"/><path d="M12 8v4l2.6 1.8"/></svg><span>Clock</span></a>
    <a href="#" id="group-weather" onclick="event.preventDefault(); handleGroupClick('weather')"><svg viewBox="0 0 24 24"><path d="M7 18h10a4 4 0 0 0 0-8 5.5 5.5 0 0 0-10.6 1.5A3.3 3.3 0 0 0 7 18z"/></svg><span>Weather</span></a>
    <a href="#" id="group-commute" onclick="event.preventDefault(); handleGroupClick('commute')"><svg viewBox="0 0 24 24"><rect x="5" y="4" width="14" height="14" rx="4"/><path d="M5 14h14M9 18l-2 3M15 18l2 3"/><circle cx="9" cy="10" r="1"/><circle cx="15" cy="10" r="1"/></svg><span>Commute</span></a>
    <a href="#" id="group-party" onclick="event.preventDefault(); handleGroupClick('party')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2"/></svg><span>Party</span></a>
    <a href="#" id="group-racing" onclick="event.preventDefault(); handleGroupClick('racing')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.2"/><path d="M3.4 10.4l6.5 1M20.6 10.4l-6.5 1M12 14.2V21"/></svg><span>Racing</span></a>
    <a href="#" id="group-gremio" onclick="event.preventDefault(); handleGroupClick('gremio')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3.5 9 Q12 12.2 20.5 9"/><path d="M3.5 15 Q12 11.8 20.5 15"/></svg><span>Grêmio</span></a>
    <a href="#" id="group-alerts" onclick="event.preventDefault(); handleGroupClick('alerts')"><svg viewBox="0 0 24 24"><path d="M12 3 2.5 20h19L12 3z"/><path d="M12 9.5v4.5"/><circle cx="12" cy="17" r="1" fill="currentColor" stroke="none"/></svg><span>Alerts</span></a>
    </div>
    </div>
    <script>
        let currentModeFromServer = 'unknown'; let localAnimationId = null;
        // Tiles are groups; the Pi only knows the flat modes inside them.
        const GROUPS = {{
            auto: {{name: 'Traffic light', modes: ['auto']}},
            weather: {{name: 'Weather', modes: ['temp', 'rain', 'uv', 'biergarten', 'space']}},
            commute: {{name: 'Commute', modes: ['s_bahn', 'stau']}},
            alerts: {{name: 'Alerts', modes: ['emergency', 'sos']}},
            party: {{name: 'Party', modes: ['party', 'audio_vu']}},
            racing: {{name: 'Racing', modes: ['race_start', 'racing', 'hotlap']}},
            gremio: {{name: 'Grêmio', modes: ['gremio']}},
            clock: {{name: 'Clock', modes: ['clock', 'westminster']}},
        }};
        // Sub-mode name and colour ranges (r/y/g/a = red/yellow/green/all), from the Pi's own settings.
        const SUBS = {{
            biergarten: {{name: 'Biergarten', rule: [['g', '18°C+, clear, from 16h'], ['y', '15–18°C or cloudy'], ['r', 'rain, cold, before 16h']]}},
            uv: {{name: 'UV', rule: [['g', 'UV under 3'], ['y', '3–5'], ['r', '6+ (blinks at 8+)']]}},
            space: {{name: 'Space', rule: [['g', 'Kp 3 or less'], ['y', 'Kp 4'], ['r', 'Kp 5+ storm (blinks)']]}},
            temp: {{name: 'Temperature', chip: 'Temp', rule: [['g', '{TEMP_GREEN_RANGE[0]:g}–{TEMP_GREEN_RANGE[1]:g}°C'], ['y', '{TEMP_YELLOW_RANGE[0]:g}–{TEMP_GREEN_RANGE[0]:g} or {TEMP_GREEN_RANGE[1]:g}–{TEMP_YELLOW_RANGE[1]:g}°C'], ['r', 'below {TEMP_YELLOW_RANGE[0]:g} or above {TEMP_YELLOW_RANGE[1]:g}°C']]}},
            rain: {{name: 'Rain', rule: [['g', 'dry next {RAIN_YELLOW_MINUTES // 60} h'], ['y', 'rain within {RAIN_YELLOW_MINUTES // 60} h'], ['r', 'raining or within {RAIN_RED_MINUTES} min']]}},
            s_bahn: {{name: 'S-Bahn', rule: [['g', '13+ min'], ['y', '10–12 min (blinks at 9)'], ['r', 'under 9 min']]}},
            stau: {{name: 'Stau', rule: [['g', 'delay under 20%'], ['y', '20–45%'], ['r', '45%+']]}},
            emergency: {{name: 'Blinking', rule: [['y', 'blinks every {EMERGENCY_BLINK_INTERVAL:g} s']]}},
            sos: {{name: 'SOS', rule: [['a', 'Morse SOS on all lamps']]}},
            racing: {{name: 'Revs', rule: [['g', 'shift light'], ['y', 'shift now (green + yellow)'], ['r', 'redline (all)']]}},
            race_start: {{name: 'Start', rule: [['r', 'start lights on'], ['y', 'get ready or yellow flag'], ['g', 'go or green flag']]}},
            hotlap: {{name: 'Hotlap', rule: [['g', 'faster than your best lap'], ['y', 'slower'], ['r', 'lap invalid']]}},
            clock: {{name: 'Silent', rule: [['a', 'lamp sweeps every 15 min'], ['y', 'one blink per hour']]}},
            westminster: {{name: 'Westminster', rule: [['a', 'Big Ben quarters on the speaker'], ['y', 'hour bell']]}},
            party: {{name: 'Music', rule: [['a', 'random lamp on every beat'], ['a', 'dark in silence']]}},
            audio_vu: {{name: 'VU meter', rule: [['g', 'quiet'], ['y', 'loud (green + yellow)'], ['r', 'very loud (all)']]}},
        }};
        const GROUP_OF = {{}};
        Object.entries(GROUPS).forEach(([g, d]) => d.modes.forEach(m => GROUP_OF[m] = g));
        let lastSub = {{}};
        try {{ lastSub = JSON.parse(localStorage.getItem('tl_lastSub') || '{{}}'); }} catch (e) {{}}
        function rememberSub(mode) {{
            const g = GROUP_OF[mode];
            if (!g || GROUPS[g].modes.length < 2 || lastSub[g] === mode) return;
            lastSub[g] = mode;
            try {{ localStorage.setItem('tl_lastSub', JSON.stringify(lastSub)); }} catch (e) {{}}
        }}
        function renderGroups(mode) {{
            Object.keys(GROUPS).forEach(g => document.getElementById(`group-${{g}}`).classList.toggle('active', GROUP_OF[mode] === g));
            const g = GROUP_OF[mode], subs = document.getElementById('subs');
            if (!g || GROUPS[g].modes.length < 2) {{ subs.classList.add('empty'); return; }}
            subs.classList.remove('empty');
            document.getElementById('chips').innerHTML = GROUPS[g].modes.map(m =>
                `<button type="button" class="chip${{m === mode ? ' active' : ''}}" onclick="selectSub('${{m}}')">${{SUBS[m].chip || SUBS[m].name}}</button>`).join('');
            document.querySelector('#chips .active')?.scrollIntoView({{inline: 'nearest', block: 'nearest'}});
            const dot = {{r: '#ff2b2b', y: '#ffcb2e', g: '#2bd672'}};
            document.getElementById('rule').innerHTML = SUBS[mode].rule.map(([c, txt]) =>
                `<span><b style="background:${{c === 'a' ? 'linear-gradient(#ff2b2b,#ffcb2e,#2bd672)' : dot[c]}}"></b>${{txt}}</span>`).join('');
        }}
        const pad2 = n => n.toString().padStart(2, '0');
        function clockNextChime() {{
            const d = new Date();
            const next = new Date(d); next.setMinutes((Math.floor(d.getMinutes() / 15) + 1) * 15, 0, 0);
            const h12 = next.getHours() % 12 || 12;
            const n = next.getMinutes() === 0 ? `4 sweeps + ${{h12}} blink${{h12 === 1 ? '' : 's'}}` : `${{next.getMinutes() / 15}} sweep${{next.getMinutes() === 15 ? '' : 's'}}`;
            const quiet = next.getHours() >= {CLOCK_QUIET_HOURS[0]} || next.getHours() < {CLOCK_QUIET_HOURS[1]};
            return `Next: ${{pad2(next.getHours())}}:${{pad2(next.getMinutes())}} · ${{quiet ? 'quiet hours' : n}}`;
        }}
        function clockTitle() {{
            const d = new Date();
            return `CLOCK <em>· ${{pad2(d.getHours())}}:${{pad2(d.getMinutes())}}:${{pad2(d.getSeconds())}}</em>`;
        }}
        // The Pi only pushes status when something changes, so the clock ticks on its own.
        setInterval(() => {{
            if (currentModeFromServer !== 'clock' && currentModeFromServer !== 'westminster') return;
            document.querySelector('#modeText strong').innerHTML = clockTitle();
            document.getElementById('info-display').textContent = clockNextChime();
        }}, 1000);
        function modeTitle(mode) {{
            if (mode === 'idle') return 'OFF';
            if (mode === 'clock' || mode === 'westminster') return clockTitle();
            const g = GROUP_OF[mode];
            if (!g) return mode.replace('_', ' ').toUpperCase();
            const name = GROUPS[g].name.toUpperCase();
            return GROUPS[g].modes.length > 1 ? `${{name}} <em>· ${{SUBS[mode].name}}</em>` : name;
        }}
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
        function computeBarPct(mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level) {{
            if (mode === 'racing') {{
                if (simhub_rev_pct === undefined || simhub_rev_pct === null) return null;
                return Math.max(0, Math.min(1, simhub_rev_pct / 100));
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
            if (mode === 'hotlap') {{
                const d = lastStatus.simhub_delta;
                return (lastStatus.simhub_live && typeof d === 'number' && !lastStatus.simhub_lap_invalid) ? Math.min(1, Math.abs(d)) : null;
            }}
            if (mode === 'temp') {{
                const v = (environment || {{}}).temp;
                return (typeof v === 'number') ? Math.max(0, Math.min(1, (v + 5) / 40)) : 1;
            }}
            if (mode === 'party') {{
                const v = (party || {{}}).level;
                return (typeof v === 'number') ? Math.max(0, Math.min(1, v)) : null;
            }}
            if (mode === 's_bahn') {{
                if (s_bahn_minutes === -1) return 1;
                return Math.max(0, Math.min(1, 1 - Math.max(0, Math.min(20, s_bahn_minutes)) / 20));
            }}
            return null;
        }}
        function updateBar(color, mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level) {{
            const ledStrip = document.getElementById('ledStrip');
            const pct = computeBarPct(mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level);
            if (pct === null) {{
                ledStrip.classList.remove('visible');
                return;
            }}
            ledStrip.classList.add('visible');
            // In racing mode the bar tracks rev_pct continuously even while the main
            // lights are still black (below the first shift point) - fall back to green.
            const hex = barHexForColor(color) || (mode === 'racing' || mode === 'party' || mode === 'audio_vu' ? '#00ff00' : null);
            const raw = hex ? pct * LED_SEGMENTS.length : 0;
            const fullLit = Math.floor(raw);
            const frac = raw - fullLit;
            LED_SEGMENTS.forEach((seg, i) => {{
                const level = i < fullLit ? 1 : (i === fullLit ? frac : 0);
                const lit = level > 0.03;
                seg.classList.toggle('lit', lit);
                seg.style.background = lit ? hex : '#2a2d33';
                seg.style.opacity = lit ? (0.35 + 0.65 * level) : 1;
            }});
        }}
        function updateVisuals(color, mode, s_bahn_minutes, race_step, space_weather, traffic, party, s_bahn_transit, environment, gremio, simhub_rev_pct, audio_level) {{
            updateBar(color, mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level);
            if (currentModeFromServer !== mode) {{
                rememberSub(mode);
                renderGroups(mode);
                document.querySelector('#modeText strong').innerHTML = modeTitle(mode);
            }}
            currentModeFromServer = mode;
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
                const e = environment || {{}};
                if (typeof e.temp === 'number' && e.condition) {{ infoDisplay.textContent = `${{e.temp.toFixed(1)}}°C, ${{e.condition}}`; }}
                else {{ infoDisplay.textContent = 'No weather data.'; }}
            }}
            else if (mode === 'race_start') {{
                const names = {{red: 'Start lights on', yellow: 'Get ready / yellow flag', green: 'Go! / green flag', black: 'No start or flag right now'}};
                infoDisplay.textContent = lastStatus.simhub_live ? (names[lastStatus.simhub_flag] || '') : 'Waiting for SimHub…';
            }}
            else if (mode === 'hotlap') {{
                const d = lastStatus.simhub_delta;
                if (!lastStatus.simhub_live) infoDisplay.textContent = 'Waiting for SimHub…';
                else if (lastStatus.simhub_lap_invalid) infoDisplay.textContent = 'Lap invalid. Next lap counts again.';
                else if (typeof d !== 'number') infoDisplay.textContent = 'Set a lap first: no reference yet.';
                else infoDisplay.textContent = `${{d < 0 ? '−' : '+'}}${{Math.abs(d).toFixed(2)}} s vs your best lap`;
            }}
            else if (mode === 'racing' && race_step >= 4) {{
                infoDisplay.textContent = (typeof simhub_rev_pct === 'number') ? `Revs: ${{Math.round(simhub_rev_pct)}}%` : 'Waiting for SimHub…';
            }}
            else if (mode === 'space') {{
                if (space_weather && space_weather.kp_index !== undefined) {{ infoDisplay.textContent = `Kp-index: ${{space_weather.kp_index}} (${{space_weather.condition}})`; }}
                else {{ infoDisplay.textContent = 'No space weather data.'; }}
            }}
            else if (mode === 'stau') {{
                if (traffic && traffic.commute_time) {{ infoDisplay.textContent = `By car: ${{traffic.commute_time}}`; }}
                else {{ infoDisplay.textContent = 'No traffic data.'; }}
            }}
            else if (mode === 'party') {{
                const p = party || {{}};
                const labels = {{silence: 'Waiting for music…', ambient: 'Listening for a beat…', chill: 'Chill', groove: 'Groove', hype: 'Hype'}};
                let txt = labels[p.vibe] || 'Starting…';
                if (p.bpm) txt = `${{Math.round(p.bpm)}} BPM · ${{txt}} · lock ${{Math.round((p.confidence || 0) * 100)}}%`;
                infoDisplay.textContent = txt;
            }}
            else if (mode === 'uv') {{
                const e = environment || {{}};
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
                if (typeof e.temp === 'number' && e.condition) {{
                    parts.push(`${{e.temp.toFixed(0)}}°C ${{e.condition}}`);
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
            else if (mode === 'audio_vu') {{
                infoDisplay.textContent = `Audio level: ${{Math.round(Math.min(Math.max(audio_level || 0, 0), 1) * 100)}}%`;
            }}
            else if (mode === 'temp') {{
                const e = environment || {{}};
                if (typeof e.temp !== 'number') infoDisplay.textContent = 'No temperature data.';
                else {{
                    const parts = [`${{Math.round(e.temp)}}°C now`];
                    if (typeof e.feels_like === 'number') parts.push(`feels ${{Math.round(e.feels_like)}}°C`);
                    if (typeof e.temp_max === 'number') parts.push(`high ${{Math.round(e.temp_max)}}°C`);
                    infoDisplay.textContent = parts.join(' · ');
                }}
            }}
            else if (mode === 'rain') {{
                const e = environment || {{}};
                if (e.rain_now === undefined) infoDisplay.textContent = 'No forecast data.';
                else if (e.rain_now) infoDisplay.textContent = 'Raining now.';
                else if (typeof e.rain_eta_min === 'number') {{
                    const m = e.rain_eta_min;
                    const when = m < 60 ? `${{Math.max(m, 5)}} min` : `${{(m / 60).toFixed(m < 120 ? 1 : 0)}} h`;
                    infoDisplay.textContent = `Rain likely in ~${{when}} (${{e.rain_prob_max}}%).`;
                }}
                else infoDisplay.textContent = `Dry for the next {RAIN_YELLOW_MINUTES // 60} h (max ${{e.rain_prob_max || 0}}%).`;
            }}
            else if (mode === 'clock' || mode === 'westminster') {{
                infoDisplay.textContent = clockNextChime();
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
                        const d2 = new Date(g.date.length > 10 ? g.date : g.date + 'T12:00Z');
                        dateStr = ` · ${{days[d2.getDay()]}} ${{d2.getDate()}} ${{months[d2.getMonth()]}}`;
                    }}
                    parts.push(`Last: ${{tag}} ${{g.score}} ${{vs}} ${{g.opponent || '?'}}${{dateStr}}`);
                }}
                if (g.next_match_date) {{
                    const d = new Date(g.next_match_date);
                    const hh = d.getHours().toString().padStart(2,'0');
                    const mm = d.getMinutes().toString().padStart(2,'0');
                    const vs2 = g.next_match_home_away === 'home' ? 'vs' : '@';
                    parts.push(`Next: ${{vs2}} ${{g.next_match_opponent || '?'}} · ${{days[d.getDay()]}} ${{d.getDate()}} ${{months[d.getMonth()]}} ${{hh}}:${{mm}}`);
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
                    happy:   {{mouth:'M12,24 Q20,34 28,24', angry:false}},
                    neutral: {{mouth:'M12,25 L28,25', angry:false}},
                    angry:   {{mouth:'M12,29 Q20,20 28,29', angry:true}},
                }};
                const key = result === 'win' ? 'happy' : result === 'loss' ? 'angry' : (result === 'draw' ? 'neutral' : null);
                const activeFace = isGreenOn ? gremioFaces[2] : isYellowOn ? gremioFaces[1] : isRedOn ? gremioFaces[0] : null;
                if (activeFace && key) {{
                    const e = EXPRESSIONS[key];
                    const eyeY = e.angry ? 15 : 14;
                    const brows = e.angry
                        ? `<line x1="6" y1="7" x2="14" y2="9" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/><line x1="34" y1="7" x2="26" y2="9" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>`
                        : '';
                    activeFace.innerHTML = `${{brows}}<circle cx="9" cy="${{eyeY}}" r="3.2" fill="#fff"/><circle cx="31" cy="${{eyeY}}" r="3.2" fill="#fff"/><path d="${{e.mouth}}" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>`;
                    activeFace.classList.add('visible');
                }}
            }} else {{
                tlBody.classList.remove('gremio-theme');
                gremioStars.classList.remove('visible');
            }}
        }}
        function stopLocalAnimation() {{ if (localAnimationId) {{ clearInterval(localAnimationId); clearTimeout(localAnimationId); localAnimationId = null; }} }}
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
                const step = sosPattern[sosIndex]; updateVisuals(step.state, 'sos', -1, 0, {{}}, {{}}, {{}}, {{}}, {{}}, {{}});
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
            if (!isTogglingOff && mode === 'sos') startSosAnimation();
        }}
        function handleGroupClick(g) {{
            // Tapping the active group turns it off (the Pi toggles a mode set twice);
            // otherwise start the sub-mode used last.
            // A remembered sub-mode that no longer exists falls back to the first one.
            const remembered = GROUPS[g].modes.includes(lastSub[g]) ? lastSub[g] : GROUPS[g].modes[0];
            const mode = GROUP_OF[currentModeFromServer] === g ? currentModeFromServer : remembered;
            handleModeClick(mode);
        }}
        function selectSub(mode) {{
            if (mode !== currentModeFromServer) handleModeClick(mode);
        }}
        function setConnected(ok) {{
            const pill = document.getElementById('connPill');
            const label = document.getElementById('connLabel');
            pill.className = ok ? 'conn-pill live' : 'conn-pill lost';
            label.textContent = ok ? 'Live' : 'Reconnecting…';
        }}
        let lastStatus = {{}};
        function applyStatus(status) {{
            lastStatus = status;
            if (localAnimationId) return;
            updateVisuals(status.color, status.mode, status.s_bahn_minutes, status.race_step, status.space_weather, status.traffic, status.party, status.s_bahn_transit, status.environment, status.gremio, status.simhub_rev_pct, status.audio_level);
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
        last_payload = None; last_write = 0.0
        try:
            while self.controller.state.running:
                # Check 20x a second but only write when something changed, plus a
                # keepalive now and then, so an idle phone's radio can sleep.
                payload = json.dumps(self.controller.get_status()); now = time()
                if payload != last_payload:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    last_payload = payload
                elif now - last_write >= SSE_KEEPALIVE_SECONDS:
                    self.wfile.write(b": ping\n\n")
                else:
                    sleep(0.05); continue
                self.wfile.flush(); last_write = now
                sleep(0.05)
        except OSError:  # browser gone: broken pipe, reset, timeout
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
                elif act=='chime':
                    # Test a chime: quarter 1-3 = :15/:30/:45, 4 = full hour; hour 1-12.
                    quarter = int(q.get('quarter',['4'])[0]); hour = int(q.get('hour',['12'])[0])
                    if not (1 <= quarter <= 4 and 1 <= hour <= 12): raise ValueError("quarter 1-4, hour 1-12")
                    self.controller.test_chime(quarter, hour)
                elif act=='party_record':
                    with self.controller.state.lock: self.controller.state.mode_state['party_record'] = True
                self.send_response(200); self.end_headers(); return
            except Exception as e:
                logging.warning(f"action error {act}: {e}"); self._err(400, str(e)); return
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
            'hotlap': handle_hotlap_mode,
            'race_start': handle_race_start_mode,
            'space': handle_space_mode,
            'stau': handle_stau_mode,
            'uv': handle_uv_mode,
            'gremio': handle_gremio_mode,
            'clock': handle_clock_mode,
            'westminster': handle_westminster_mode,
            'audio_vu': handle_audio_vu_mode,
            'temp': handle_temp_mode,
            'rain': handle_rain_mode,
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
            if self.state.current_color == color or not self.state.running: return
            prev = self.state.current_color
            try:
                self.hardware.set_state(color); self.state.current_color = color
                # The relay click is audible to the mic; Party masks it out,
                # the VU meter ignores the mic until it has rung out.
                now = time()
                self.state.mode_state['click_until'] = now + PARTY_CLICK_MASK_SECONDS
                if self.state.current_mode == 'audio_vu':
                    self.state.mode_state['audio_vu_last_step_time'] = now
                    self.state.mode_state['audio_vu_ignore_until'] = now + AUDIO_VU_RELAY_IGNORE_SECONDS
                logging.info(f"Light: {prev} -> {color}")
            except Exception as e:
                logging.error(f"Failed to set light {color}: {e}")
    def set_mode(self, mode: str) -> None:
        if mode not in self.mode_handlers and mode not in ('manual', 'idle'):
            raise ValueError(f"unknown mode {mode!r}")
        with self.state.lock:
            if self.state.current_mode == mode:
                self.state.target_mode = 'idle'; logging.info(f"Toggle off mode {mode}")
            else:
                self.state.target_mode = mode; logging.info(f"Switch to mode {mode}")
            self._remember_choice()
    def set_manual_color(self, color: str) -> None:
        if color not in LIGHT_STATES:
            raise ValueError(f"unknown color {color!r}")
        with self.state.lock:
            if self.state.current_mode == 'manual' and self.state.current_color == color:
                self.state.target_manual_color = 'off'
            else:
                self.state.target_manual_color = color
            self.state.target_mode = 'manual'; logging.info(f"Manual color {color}")
            self._remember_choice()
    def _remember_choice(self) -> None:
        # Caller holds the lock.
        self.state.chosen_mode = self.state.target_mode; self.state.chosen_color = self.state.target_manual_color
        try:
            with open(LAST_MODE_FILE, 'w', encoding='utf-8') as f:
                json.dump({'mode': self.state.chosen_mode, 'color': self.state.chosen_color}, f)
        except OSError as e:
            logging.warning(f"Could not save the mode to {LAST_MODE_FILE}: {e}")
    def load_choice(self) -> None:
        try:
            with open(LAST_MODE_FILE, encoding='utf-8') as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        mode, color = saved.get('mode'), saved.get('color', 'off')
        if (mode in self.mode_handlers or mode in ('manual', 'idle')) and color in LIGHT_STATES:
            with self.state.lock:
                self.state.chosen_mode, self.state.chosen_color = mode, color
            logging.info(f"Last mode: {mode}" + (f" ({color})" if mode == 'manual' else ''))
    def restore_choice(self) -> None:
        # Caller holds the lock.
        self.state.target_mode = self.state.chosen_mode; self.state.target_manual_color = self.state.chosen_color
    def test_chime(self, quarter: int, hour12: int) -> None:
        with self.state.lock:
            self.state.mode_state['clock_force'] = (quarter, hour12)
            if self.state.target_mode not in ('clock', 'westminster'):
                self.state.target_mode = 'westminster'; self._remember_choice()
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
        stop_chime()  # leaving Westminster mid-chime silences the bells
        self.state.current_mode = new_mode; self.state.last_state_change_time = time()
        if new_mode == 'auto':
            # Start in green so the first visible cycle isn't a 20s red hold.
            self.set_light_state('green'); self.state.mode_state['next_auto_state'] = 'red'
        elif new_mode == 'sos':
            self.state.mode_state['sos_index'] = 0
            # Apply the first pattern step on entry; handler advances after its duration.
            self.set_light_state(self.state.mode_state['sos_pattern'][0]['state'])
        elif new_mode in RACING_INTROS:
            self.state.mode_state.update(intro_seq=RACING_INTROS[new_mode], intro_idx=-1, intro_until=0.0)
        elif new_mode == 'audio_vu':
            now = time(); ms = self.state.mode_state
            self.set_light_state('off'); self.state.audio_vu_level = 0.0
            ms.update(audio_vu_last=now, audio_vu_ignore_until=now + AUDIO_VU_RELAY_IGNORE_SECONDS,
                      audio_vu_last_step_time=now, audio_vu_floor=AUDIO_VU_FLOOR_SEED, audio_vu_last_update=0.0)
        elif new_mode == 'party':
            self.state.party_status = {}
            for k in ('party_last_beat', 'party_last_switch'): self.state.mode_state[k] = 0.0
            self.state.mode_state['party_beat_count'] = 0
        elif new_mode in ('clock', 'westminster'):
            # No last chime yet: the handler chimes the current time right away.
            self.state.mode_state['clock_seq'] = None; self.state.mode_state['clock_last_key'] = None
        elif new_mode == 'idle':
            self.set_light_state('off')
    def shutdown(self):
        logging.info("Shutdown initiated")
        # Under the lock, so no handler is mid-switch; set_light_state refuses once running is off.
        with self.state.lock:
            self.state.running = False
            self.hardware.cleanup()
        logging.info("Shutdown complete")

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
    # Offline it runs the traffic light cycle; online, whatever was picked last.
    ctrl.load_choice()
    try:
        connected = _is_network_connected()
        with ctrl.state.lock:
            ctrl.state.network_connected = connected
            if connected:
                ctrl.restore_choice()
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
        threading.Thread(target=space_weather_monitor, args=(ctrl,), name='space', daemon=True),
        threading.Thread(target=traffic_monitor, args=(ctrl,), name='traffic', daemon=True),
        threading.Thread(target=simhub_udp_listener, args=(ctrl,), name='simhub', daemon=True),
        threading.Thread(target=audio_monitor, args=(ctrl,), name='audio', daemon=True),
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
